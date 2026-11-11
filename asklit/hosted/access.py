"""Revocable access capabilities. Public GETs never create conversations or run models."""

import hashlib
import hmac
import secrets
import re
import time
import unicodedata
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from .models import (
    AccessGrant,
    BotAlias,
    Identity,
    LoginSession,
    LocalAccount,
    Membership,
    Organization,
    OrganizationAlias,
    PasswordKey,
    RateBucket,
)
from .security import digest, login_session, token


def safe_next(value):
    value = str(value or "")
    parsed = urlsplit(value or "")
    return (
        value
        if not parsed.scheme
        and not parsed.netloc
        and not any(c in value for c in ["\\", "\r", "\n"])
        and not any(part in {".", ".."} for part in parsed.path.split("/"))
        and "//" not in parsed.path
        and (parsed.path.startswith(("/o/", "/a/", "/gallery"))
             or re.fullmatch(r"/[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*", parsed.path))
        else "/"
    )


def ensure_links(db, bot):
    org = db.scalar(
        select(Organization).where(Organization.id == bot.org_id).with_for_update()
    )
    if not org.slug:
        org.slug = "org-" + org.id
        alias = db.get(OrganizationAlias, org.slug)
        if alias and alias.org_id != org.id:
            raise HTTPException(409, "Choose an organization link before publishing.")
        if not alias:
            db.add(OrganizationAlias(slug=org.slug, org_id=org.id))
    if not bot.slug:
        name = unicodedata.normalize("NFKD", bot.title).encode("ascii", "ignore").decode()
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80].rstrip("-")
        if len(base) < 2:
            base = "asklit" if not base else "asklit-" + base
        # Keep legacy identifier URLs reserved; aliases are never recycled.
        if re.fullmatch(r"asklit-[a-f0-9]{32}", base):
            base = "guide-" + base
        base = base[:80].rstrip("-")
        candidate, number = base, 1
        while db.get(BotAlias, (org.id, candidate)):
            number += 1
            suffix = "-" + str(number)
            candidate = base[:80 - len(suffix)].rstrip("-") + suffix
        bot.slug = candidate
        db.add(BotAlias(org_id=org.id, slug=bot.slug, bot_id=bot.id))
    db.flush()
    return org


def public_path(db, bot):
    org = db.get(Organization, bot.org_id)
    return f"/{org.slug or 'org-' + org.id}/{bot.slug or 'asklit-' + bot.id}"


def matches_organization(db, org, user_id, settings):
    identities = list(db.scalars(select(Identity).where(Identity.user_id == user_id)))
    oauth = [
        i
        for i in identities
        if i.issuer == "https://accounts.google.com"
        or i.issuer.startswith("https://login.microsoftonline.com/")
    ]
    local = db.get(LocalAccount, user_id)
    if db.get(Membership, (org.id, user_id)) and (oauth or settings.dev_mode or (local and local.enabled)):
        return True
    # Google's signed Workspace domain is authoritative; third-party email strings
    # are not. For Microsoft use signed tenant identity, never preferred_username.
    for identity in oauth:
        if (
            identity.issuer == "https://accounts.google.com"
            and identity.email_verified
            and identity.hosted_domain
            and identity.hosted_domain.lower() in org.allowed_domains
        ):
            return True
        if identity.issuer.startswith("https://login.microsoftonline.com/"):
            tenant = identity.issuer.removeprefix(
                "https://login.microsoftonline.com/"
            ).removesuffix("/v2.0")
            if tenant.lower() in org.microsoft_tenants:
                return True
    return False


def grant_valid(db, bot, grant, settings, check_expiry=True):
    if (
        not grant
        or grant.bot_id != bot.id
        or grant.access_version != bot.access_version
        or (check_expiry and grant.expires <= time.time())
    ):
        return False
    if bot.access_mode == "password":
        key = db.get(PasswordKey, grant.password_id) if grant.password_id else None
        return bool(
            key
            and key.bot_id == bot.id
            and key.enabled
            and key.version == grant.password_version
        )
    if bot.access_mode == "organization":
        session = (
            db.get(LoginSession, grant.session_hash) if grant.session_hash else None
        )
        return bool(
            session
            and session.expires > time.time()
            and session.user_id == grant.user_id
            and matches_organization(
                db, db.get(Organization, bot.org_id), grant.user_id, settings
            )
        )
    return False


def require_access(db, bot, raw, settings):
    if bot.access_mode in {"public", "unlisted"}:
        return None
    grant = db.get(AccessGrant, digest(raw)) if raw else None
    if not grant_valid(db, bot, grant, settings):
        raise HTTPException(
            403, "Access is required or has expired. Unlock this AskLit again."
        )
    return grant


def conversation_allowed(db, bot, conversation, settings):
    if conversation.access_version != bot.access_version:
        return False
    if bot.access_mode in {"public", "unlisted"}:
        return True
    grant = (
        db.get(AccessGrant, conversation.access_grant)
        if conversation.access_grant
        else None
    )
    return grant_valid(db, bot, grant, settings, check_expiry=False)


def password_lookup(password, settings):
    return hmac.new(
        settings.secret_key.encode(),
        b"asklit-password\0" + password.encode(),
        hashlib.sha256,
    ).hexdigest()


def hash_password(value):
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(value.encode(), salt=salt, n=16384, r=8, p=1).hex()
    return salt.hex() + ":" + hashed


def verify_password(value, stored):
    salt, hashed = stored.split(":")
    candidate = hashlib.scrypt(
        value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    return secrets.compare_digest(candidate, hashed)


def access_attempt(db, request, settings, failed=False):
    # One committed bucket per client, shared across bots, bounds password probing.
    ip = request.client.host if request.client else "unknown"
    hour = int(time.time() // 3600)
    key = (
        "access:"
        + hmac.new(
            settings.secret_key.encode(), f"{ip}:{hour}".encode(), hashlib.sha256
        ).hexdigest()
    )
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(
        insert(RateBucket)
        .values(key=key, count=0, expires=(hour + 2) * 3600)
        .on_conflict_do_nothing()
    )
    row = db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
    if row.count >= 60:
        return False
    if failed:
        row.count += 1
    return True


def issue_access(db, bot, request, password, settings):
    raw = token()
    grant = AccessGrant(
        token_hash=digest(raw),
        bot_id=bot.id,
        access_version=bot.access_version,
        expires=time.time() + 3600,
    )
    if bot.access_mode == "password":
        key = db.scalar(
            select(PasswordKey).where(
                PasswordKey.bot_id == bot.id,
                PasswordKey.lookup == password_lookup(password, settings),
                PasswordKey.enabled.is_(True),
            )
        )
        if not key or not verify_password(password, key.password_hash):
            raise HTTPException(403, "That password is not active for this AskLit.")
        grant.password_id = key.id
        grant.password_version = key.version
    elif bot.access_mode == "organization":
        session = login_session(db, request, mutation=True)
        if not matches_organization(
            db, db.get(Organization, bot.org_id), session.user_id, settings
        ):
            raise HTTPException(
                403, "Your signed-in account does not match this organization."
            )
        grant.user_id = session.user_id
        grant.session_hash = session.token_hash
        grant.expires = min(grant.expires, session.expires)
    else:
        return ""
    db.add(grant)
    return raw
