"""Production password accounts, managed by their creating organization."""
import hashlib
import hmac
import re
import secrets
import time
from threading import BoundedSemaphore
from typing import Literal

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .access import safe_next
from .models import LocalAccount, LoginSession, Membership, Organization, OrganizationAlias, RateBucket, User
from .security import audit, digest, login_session, membership, token


def username(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) or len(value) > 320 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Enter an email address.")
    return value


PASSWORD_SLOTS = BoundedSemaphore(4)

def derive_password(value, salt):
    with PASSWORD_SLOTS:
        return hashlib.scrypt(value.encode(), salt=salt, n=32768, r=8, p=3, maxmem=64 * 1024 * 1024)

def password_hash(value):
    salt = secrets.token_bytes(16)
    hashed = derive_password(value, salt)
    return salt.hex() + ":" + hashed.hex()


def password_matches(value, stored):
    try:
        salt, expected = stored.split(":")
        actual = derive_password(value, bytes.fromhex(salt)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# Missing usernames perform the same password work as existing accounts.
DUMMY_HASH = "00" * 16 + ":" + "00" * 64


class NewAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=200)
    role: Literal["member", "admin"] = "member"

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        return username(value)


class ResetPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=12, max_length=200)


class ChangePassword(ResetPassword):
    current_password: str = Field(min_length=1, max_length=200)


class AccountState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def reserve_login(store, request, settings, email, *, management=False):
    """Committed attempt counters survive a rejected login/password check."""
    ip = request.client.host if request.client else "unknown"
    period = int(time.time() // 900)
    limited = False
    with store.transaction() as db:
        prefix = "manage" if management else "login"
        limits = [("ip", ip, 300 if management else 100),
                  ("pair", email + "\0" + ip, 100 if management else 10),
                  ("user", email, 500 if management else 200)]
        for kind, value, limit in limits:
            hashed = hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]
            key = f"{prefix}:{kind}:{hashed}:{period}"
            if db.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            db.execute(insert(RateBucket).values(key=key, count=0, expires=time.time()+1800).on_conflict_do_nothing())
            bucket = db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
            limited = limited or bucket.count >= limit
            bucket.count += 1
    if limited:
        raise HTTPException(429, "Too many sign-in attempts. Try again in fifteen minutes.")


def new_local_user(db, org_id, email, name, hashed, role):
    user = User(email=email, name=name)
    db.add(user)
    db.flush()
    db.add(LocalAccount(user_id=user.id, username=email, managed_org_id=org_id, password_hash=hashed))
    db.add(Membership(org_id=org_id, user_id=user.id, role=role))
    db.flush()
    return user


def bootstrap_admin(store, email, name, org_name, slug, password):
    """Operator-only CLI provisioning. Never invoked by a public request."""
    email = username(email)
    if not 12 <= len(password) <= 200:
        raise ValueError("Use a password between 12 and 200 characters.")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or not 2 <= len(slug) <= 80:
        raise ValueError("Invalid organization slug.")
    with store.transaction() as db:
        if db.scalar(select(LocalAccount).where(LocalAccount.username == email)) or db.get(OrganizationAlias, slug):
            raise ValueError("That local account or organization link already exists; nothing was changed.")
        org = Organization(name=org_name, slug=slug, gallery_enabled=True, local_accounts_enabled=True)
        db.add(org); db.flush()
        db.add(OrganizationAlias(org_id=org.id, slug=slug))
        user = new_local_user(db, org.id, email, name, password_hash(password), "owner")
        db.get(LocalAccount, user.id).must_change_password = False
        audit(db, user.id, org.id, "local.bootstrap", user.id)
        return user.id, org.id


def local_account_router(settings, store):
    router = APIRouter()

    @router.post("/auth/password/login")
    def sign_in(request: Request, email: str = Form(..., max_length=320), password: str = Form(..., max_length=200), csrf: str = Form(...)):
        expected = request.session.pop("local_login_csrf", "")
        if not expected or not secrets.compare_digest(csrf.encode(), expected.encode()):
            raise HTTPException(403, "Reload the sign-in page and try again.")
        email = email.strip().lower()
        reserve_login(store, request, settings, email)
        try:
            email = username(email)
            valid_email = True
        except ValueError:
            valid_email = False
        with store.transaction() as db:
            account = db.scalar(select(LocalAccount).where(LocalAccount.username == email)) if valid_email else None
            observed = account.password_hash if account else DUMMY_HASH
            user_id = account.user_id if account else None
        valid = password_matches(password, observed)
        raw = token()
        with store.transaction() as db:
            account = db.scalar(select(LocalAccount).where(LocalAccount.user_id == user_id).with_for_update()) if user_id else None
            if not valid or not account or not account.enabled or account.password_hash != observed:
                return RedirectResponse("/?login_error=1", status_code=303)
            old = db.get(LoginSession, digest(request.cookies.get("asklit_session", "")))
            if old: db.delete(old)
            db.add(LoginSession(token_hash=digest(raw), user_id=account.user_id, csrf=token(), expires=time.time()+86400))
        destination = safe_next(request.session.get("after_login", "/"))
        request.session.clear()
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie("asklit_session", raw, httponly=True, secure=settings.secure, samesite="lax", max_age=86400)
        return response

    @router.get("/api/organizations/{org_id}/local-accounts")
    def accounts(org_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            membership(db, org_id, session.user_id, True)
            rows = db.execute(select(LocalAccount, User).join(User, User.id == LocalAccount.user_id).where(LocalAccount.managed_org_id == org_id).order_by(User.name)).all()
            return [{"id": u.id, "email": a.username, "name": u.name, "enabled": a.enabled} for a, u in rows]

    def can_create(db, request, org_id, role):
        session = login_session(db, request, True)
        caller = membership(db, org_id, session.user_id, True)
        if not db.get(Organization, org_id).local_accounts_enabled:
            raise HTTPException(403, "The operator has not enabled local account creation for this organization.")
        if role == "admin" and caller.role != "owner":
            raise HTTPException(403, "Only owners can create administrators.")
        return session

    @router.post("/api/organizations/{org_id}/local-accounts")
    def create(org_id: str, body: NewAccount, request: Request):
        with store.transaction() as db:
            actor = can_create(db, request, org_id, body.role).user_id
        reserve_login(store, request, settings, actor, management=True)
        hashed = password_hash(body.password)
        try:
            with store.transaction() as db:
                session = can_create(db, request, org_id, body.role)
                user = new_local_user(db, org_id, body.email, body.name, hashed, body.role)
                audit(db, session.user_id, org_id, "local.create", user.id)
                return {"id": user.id, "email": body.email, "name": body.name, "enabled": True}
        except IntegrityError:
            raise HTTPException(409, "That local sign-in address is already in use.") from None

    def managed(db, request, org_id, user_id):
        session = login_session(db, request, True)
        # Match the organization → account lock order for every account mutation.
        db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
        caller = membership(db, org_id, session.user_id, True)
        account = db.scalar(select(LocalAccount).where(LocalAccount.user_id == user_id, LocalAccount.managed_org_id == org_id).with_for_update())
        if not account: raise HTTPException(404, "Local account not found.")
        target = db.get(Membership, (org_id, user_id))
        if not target:
            raise HTTPException(404, "Local account not found.")
        if db.scalar(select(Membership.org_id).where(Membership.user_id == user_id, Membership.org_id != org_id).limit(1)):
            raise HTTPException(403, "This account has cross-organization membership. Ask the operator to resolve it first.")
        if target and target.role in {"owner", "admin"} and caller.role != "owner":
            raise HTTPException(403, "Only owners can manage administrator accounts.")
        return session, account, target

    @router.patch("/api/organizations/{org_id}/local-accounts/{user_id}")
    def enable(org_id: str, user_id: str, body: AccountState, request: Request):
        with store.transaction() as db:
            session, account, target = managed(db, request, org_id, user_id)
            owns_org = db.scalar(select(Membership.org_id).where(Membership.user_id == user_id, Membership.role == "owner").limit(1))
            if not body.enabled and (session.user_id == user_id or owns_org):
                raise HTTPException(409, "Keep your own account enabled. Demote other owners before disabling them.")
            account.enabled = body.enabled
            db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            audit(db, session.user_id, org_id, "local.enable" if body.enabled else "local.disable", user_id)
            return {"enabled": body.enabled}

    @router.post("/api/organizations/{org_id}/local-accounts/{user_id}/password")
    def reset(org_id: str, user_id: str, body: ResetPassword, request: Request):
        with store.transaction() as db:
            session, _, _ = managed(db, request, org_id, user_id)
            actor = session.user_id
            if actor == user_id:
                raise HTTPException(409, "Use Change password for your own account.")
        reserve_login(store, request, settings, actor, management=True)
        hashed = password_hash(body.password)
        with store.transaction() as db:
            session, account, _ = managed(db, request, org_id, user_id)
            account.password_hash = hashed
            account.must_change_password = True
            db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            audit(db, session.user_id, org_id, "local.password_reset", user_id)
            return {"changed": True}

    @router.post("/api/account/password")
    def change(body: ChangePassword, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            user_id = session.user_id
            account = db.get(LocalAccount, user_id)
            observed = account.password_hash if account else DUMMY_HASH
        reserve_login(store, request, settings, "password-change:" + user_id)
        if not password_matches(body.current_password, observed):
            raise HTTPException(400, "Current password was not accepted.")
        if secrets.compare_digest(body.password.encode(), body.current_password.encode()):
            raise HTTPException(400, "Choose a different password from the one you were given.")
        hashed = password_hash(body.password)
        with store.transaction() as db:
            account = db.scalar(select(LocalAccount).where(LocalAccount.user_id == user_id).with_for_update())
            session = login_session(db, request, True)
            if not account or account.password_hash != observed:
                raise HTTPException(409, "The password changed. Sign in again.")
            account.password_hash = hashed
            account.must_change_password = False
            db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            audit(db, user_id, account.managed_org_id, "local.password_change", user_id)
        return {"changed": True}

    return router
