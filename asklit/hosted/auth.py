"""OIDC redirects stay on the backend; the static public frontend needs no SSO."""

import secrets
import re
import time

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import LocalAccount, Identity, LoginSession, Membership, Organization, OrganizationAlias, User
from .security import digest, login_session, token
from .access import safe_next


def valid_microsoft_issuer(claims, value, tenant="organizations"):
    tid = claims.get("tid", "")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", tid):
        return False
    # Personal Microsoft accounts are outside the work/school-only scope.
    if tid.lower() == "9188040d-6c67-4c5b-b112-36a304b66dad":
        return False
    return (
        tenant == "organizations" or tid.lower() == tenant.lower()
    ) and value == f"https://login.microsoftonline.com/{tid}/v2.0"


def resolve_identity(db, claims):
    """Never merge Google/Microsoft accounts using email or a domain."""
    issuer, subject = claims["iss"], claims["sub"]
    if issuer == "accounts.google.com":
        issuer = "https://accounts.google.com"
    identity = db.get(Identity, (issuer, subject))
    if identity:
        user = db.get(User, identity.user_id)
        user.email = str(claims.get("email") or claims.get("preferred_username") or "")[
            :320
        ]
        identity.email_verified = claims.get("email_verified") is True
        identity.hosted_domain = (
            str(claims.get("hd") or "").lower()[:255]
            if issuer == "https://accounts.google.com"
            else ""
        )
        return user
    user = User(
        email=str(claims.get("email") or claims.get("preferred_username") or "")[:320],
        name=str(claims.get("name") or "AskLit user")[:200],
    )
    db.add(user)
    db.flush()
    db.add(
        Identity(
            issuer=issuer,
            subject=subject,
            user_id=user.id,
            email_verified=claims.get("email_verified") is True,
            hosted_domain=str(claims.get("hd") or "").lower()[:255]
            if issuer == "https://accounts.google.com"
            else "",
        )
    )
    org = Organization(name=f"{user.name}'s workspace")
    db.add(org)
    db.flush()
    if issuer == "urn:asklit:local-development" and not db.get(OrganizationAlias, "lit"):
        org.slug = "lit"
        org.gallery_enabled = True
        db.add(OrganizationAlias(slug="lit", org_id=org.id))
    db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
    return user


def auth_router(settings, store):
    router = APIRouter()
    oauth = OAuth()
    if settings.google_client_id:
        oauth.register(
            "google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid email profile",
                "code_challenge_method": "S256",
            },
        )
    if settings.microsoft_client_id:
        oauth.register(
            "microsoft",
            client_id=settings.microsoft_client_id,
            client_secret=settings.microsoft_client_secret,
            server_metadata_url=f"https://login.microsoftonline.com/{settings.microsoft_tenant}/v2.0/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid email profile",
                "code_challenge_method": "S256",
            },
        )

    if settings.dev_mode:

        @router.post("/auth/local/login")
        async def local_login(request: Request):
            form = await request.form()
            expected = request.session.pop("local_login_csrf", "")
            if not expected or not secrets.compare_digest(
                str(form.get("csrf", "")).encode(), expected.encode()
            ):
                raise HTTPException(403, "Reload the login page and try again.")
            username, password = (
                str(form.get("username", "")),
                str(form.get("password", "")),
            )
            valid_user = secrets.compare_digest(
                username.encode(), settings.dev_username.encode()
            )
            valid_password = secrets.compare_digest(
                password.encode(), settings.dev_password.encode()
            )
            if not (valid_user and valid_password):
                return RedirectResponse("/?login_error=1", status_code=303)
            return finish_login(
                request,
                {
                    "iss": "urn:asklit:local-development",
                    "sub": settings.dev_username,
                    "email": settings.dev_username,
                    "name": "Local developer",
                },
            )

    @router.get("/auth/{provider}/login")
    async def login(provider: str, request: Request):
        client = (
            oauth.create_client(provider)
            if provider in {"google", "microsoft"}
            else None
        )
        if client is None:
            raise HTTPException(404, "This sign-in provider is not configured.")
        if request.query_params.get("next"):
            request.session["after_login"] = safe_next(request.query_params["next"])
        return await client.authorize_redirect(
            request, f"{settings.base_url}/auth/{provider}/callback"
        )

    @router.get("/auth/{provider}/callback")
    async def callback(provider: str, request: Request):
        client = (
            oauth.create_client(provider)
            if provider in {"google", "microsoft"}
            else None
        )
        if client is None:
            raise HTTPException(404)
        try:
            options = {
                "claims_options": {
                    "iss": {
                        "values": ["https://accounts.google.com", "accounts.google.com"]
                    }
                }
            }
            if provider == "microsoft":
                options = {
                    "claims_options": {
                        "iss": {
                            "essential": True,
                            "validate": lambda c, v: valid_microsoft_issuer(
                                c, v, settings.microsoft_tenant
                            ),
                        }
                    }
                }
            result = await client.authorize_access_token(request, **options)
            claims = result.get("userinfo")
            if not claims or not claims.get("sub") or not claims.get("iss"):
                raise ValueError("Missing verified identity.")
        except Exception:
            # Do not send tokens, provider errors, or authorization codes to clients/logs.
            raise HTTPException(
                400, "Sign-in could not be verified. Start sign-in again."
            ) from None
        return finish_login(request, claims)

    def finish_login(request, claims):
        raw = token()
        for attempt in range(2):
            try:
                with store.transaction() as db:
                    user = resolve_identity(db, claims)
                    previous = db.get(
                        LoginSession, digest(request.cookies.get("asklit_session", ""))
                    )
                    if previous:
                        db.delete(previous)
                    db.add(
                        LoginSession(
                            token_hash=digest(raw),
                            user_id=user.id,
                            csrf=token(),
                            expires=time.time() + 86400,
                        )
                    )
                break
            except IntegrityError:
                if attempt:
                    raise
        destination = safe_next(request.session.get("after_login", "/"))
        request.session.clear()
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            "asklit_session",
            raw,
            httponly=True,
            secure=settings.secure,
            samesite="lax",
            max_age=86400,
        )
        return response

    @router.post("/api/logout")
    def logout(request: Request):
        with store.transaction() as db:
            session = login_session(db, request, mutation=True)
            db.delete(session)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("asklit_session")
        return response

    @router.get("/api/me")
    def me(request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            user = db.get(User, session.user_id)
            orgs = db.execute(
                select(Organization, Membership)
                .join(Membership)
                .where(Membership.user_id == user.id)
            ).all()
            return {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "csrf": session.csrf,
                "local_account": db.get(LocalAccount, user.id) is not None,
                "organizations": [
                    {"id": o.id, "name": o.name, "role": m.role} for o, m in orgs
                ],
                "models": list(settings.models),
                "model_labels": {
                    key: preset.get("label", preset["model"])
                    for key, preset in settings.models.items()
                },
                "evaluation_max_calls": settings.evaluation_max_calls,
            }

    return router
