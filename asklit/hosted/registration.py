"""Self-service local accounts during operator-controlled open registration."""
import secrets
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import Organization, OrganizationAlias, LocalAccount, LoginSession
from .local_accounts import username, password_hash, new_local_user, reserve_login
from .security import audit, token, digest


class Registration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=150)
    email: str = Field(max_length=320)
    password: str = Field(min_length=12, max_length=200)
    csrf: str = Field(min_length=1, max_length=200)

    @field_validator('email')
    @classmethod
    def clean_email(cls, value):return username(value)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        if not value.strip():raise ValueError('Enter your name.')
        return value.strip()


def registration_router(settings, store):
    router = APIRouter()

    @router.post('/api/register')
    def register(body: Registration, request: Request):
        if settings.approval_required:
            raise HTTPException(403, 'Open registration has ended. Contact litlab@suffolk.edu for hosting approval.')
        expected = request.session.get('local_login_csrf', '')
        if not expected or not secrets.compare_digest(body.csrf.encode(), expected.encode()):
            raise HTTPException(403, 'Reload the sign-in page and try again.')
        # A shared key gives both per-IP and service-wide registration ceilings,
        # including failed attempts, before expensive password hashing.
        reserve_login(store, request, settings, 'public-registration')
        with store.transaction() as db:
            if db.scalar(select(LocalAccount.user_id).where(LocalAccount.username == body.email)):
                raise HTTPException(409, 'A local account with that email already exists. Sign in instead.')
        hashed = password_hash(body.password)
        raw = token()
        try:
            with store.transaction() as db:
                if settings.approval_required:
                    raise HTTPException(403, 'Open registration has ended. Contact litlab@suffolk.edu for hosting approval.')
                org = Organization(name=f"{body.name}'s workspace", local_accounts_enabled=False)
                db.add(org);db.flush()
                org.slug = 'org-' + org.id
                db.add(OrganizationAlias(slug=org.slug, org_id=org.id))
                user = new_local_user(db, org.id, body.email, body.name, hashed, 'owner')
                db.get(LocalAccount, user.id).must_change_password = False
                # Trial access does not permanently approve hosting, verify email,
                # join an existing school, or grant platform administration.
                old = db.get(LoginSession, digest(request.cookies.get("asklit_session", "")))
                if old:db.delete(old)
                db.add(LoginSession(token_hash=digest(raw), user_id=user.id, csrf=token(), expires=time.time()+86400))
                audit(db, user.id, org.id, 'local.register', user.id)
        except IntegrityError:
            raise HTTPException(409, 'A local account with that email already exists. Sign in instead.') from None
        request.session.clear()
        response = JSONResponse({'created': True})
        response.set_cookie('asklit_session', raw, httponly=True, secure=settings.secure, samesite='lax', max_age=86400)
        return response
    return router
