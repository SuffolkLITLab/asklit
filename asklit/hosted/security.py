"""Authentication is separate from organization membership and bot ownership."""

import hashlib
import secrets
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from .models import Audit, Bot, LocalAccount, LoginSession, Membership, Organization, RateBucket, User


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token():
    return secrets.token_urlsafe(32)


def login_session(db, request, mutation=False):
    value = request.cookies.get("asklit_session", "")
    session = db.get(LoginSession, digest(value))
    if not session or session.expires <= time.time():
        raise HTTPException(401, "Please sign in.")
    local = db.get(LocalAccount, session.user_id)
    if local and not local.enabled:
        raise HTTPException(401, "Please sign in.")
    if local and local.must_change_password and request.url.path not in {"/api/me", "/api/logout", "/api/account/password"}:
        raise HTTPException(403, "Change your password before continuing.")
    settings = request.app.state.settings
    path = request.url.path
    author_routes = path.startswith("/api/") and path not in {"/api/me", "/api/logout", "/api/account/password", "/api/gallery", "/api/invitations/accept"} and not path.startswith(("/api/public/", "/api/gallery/"))
    if settings.require_author_approval and author_routes:
        user = db.get(User, session.user_id)
        if not user or not user.author_approved:
            raise HTTPException(403, "Hosting approval is required. Email litlab@suffolk.edu to request approval and discuss the hosting fee.")
    if mutation and not secrets.compare_digest(
        request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()
    ):
        raise HTTPException(403, "Invalid CSRF token. Reload this page.")
    return session


def membership(db, org_id, user_id, admin=False):
    member = db.get(Membership, (org_id, user_id))
    if not member or (admin and member.role not in {"owner", "admin"}):
        raise HTTPException(404, "Organization not found.")
    return member


def accessible_bot(db, bot_id, user_id, lock=False):
    stmt = select(Bot).where(Bot.id == bot_id, Bot.archived.is_(False))
    if lock:
        stmt = stmt.with_for_update()
    bot = db.scalar(stmt.execution_options(populate_existing=lock))
    if not bot:
        raise HTTPException(404, "Bot not found.")
    member = membership(db, bot.org_id, user_id)
    if member.role == "member" and bot.owner_id != user_id:
        raise HTTPException(404, "Bot not found.")
    return bot


def audit(db, user_id, org_id, action, target):
    db.add(Audit(user_id=user_id, org_id=org_id, action=action, target=target))


def reserve_calls(db, org_id, settings, count=1, audience="authoring"):
    """Serialize reservations across all API processes and workers."""
    org = db.scalar(
        select(Organization).where(Organization.id == org_id).with_for_update()
    )
    day = datetime.now(timezone.utc).date().isoformat()
    if org.quota_day != day:
        org.quota_day, org.calls = day, 0
    if org.calls + count > settings.daily_calls:
        raise HTTPException(
            429, "This organization's daily model-call allowance has been reached."
        )
    # A host ceiling prevents creating extra organizations to evade spending caps.
    key = f"model-calls:{audience}:{day}"
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(
        insert(RateBucket)
        .values(key=key, count=0, expires=time.time() + 172800)
        .on_conflict_do_nothing()
    )
    bucket = db.scalar(
        select(RateBucket).where(RateBucket.key == key).with_for_update()
    )
    ceiling = (
        settings.public_daily_calls
        if audience == "public"
        else settings.global_daily_calls
    )
    if bucket.count + count > ceiling:
        raise HTTPException(
            429, "The service's daily model-call allowance has been reached."
        )
    bucket.count += count
    org.calls += count
