"""Author-controlled links/access and administrator-controlled organization rules."""

import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .access import ensure_links, hash_password, password_lookup, public_path
from .models import BotAlias, Organization, OrganizationAlias, PasswordKey
from .schemas import BotSharing, NewPassword, OrganizationSharing, PasswordState
from .security import accessible_bot, audit, login_session, membership


def sharing_router(settings, store):
    router = APIRouter(prefix="/api")

    def org_view(org):
        slug = org.slug or "org-" + org.id
        return {
            "slug": slug,
            "gallery_enabled": org.gallery_enabled,
            "allowed_domains": org.allowed_domains,
            "microsoft_tenants": org.microsoft_tenants,
            "gallery_url": settings.base_url + "/" + slug,
        }

    def bot_view(db, bot):
        org = db.get(Organization, bot.org_id)
        return {
            "slug": bot.slug or "asklit-" + bot.id,
            "access_mode": bot.access_mode,
            "url": settings.base_url + public_path(db, bot),
            "organization": org_view(org),
        }

    @router.get("/organizations/{org_id}/sharing")
    def get_org(org_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            membership(db, org_id, session.user_id, True)
            return org_view(db.get(Organization, org_id))

    @router.put("/organizations/{org_id}/sharing")
    def put_org(org_id: str, body: OrganizationSharing, request: Request):
        domains = sorted(
            set(d.strip().lower().rstrip(".") for d in body.allowed_domains)
        )
        if any(
            len(d) > 253
            or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+",
                d,
            )
            for d in domains
        ):
            raise HTTPException(
                422, "Enter domains such as school.edu, without @, wildcards or URLs."
            )
        try:
            tenants = sorted(set(str(UUID(t)) for t in body.microsoft_tenants))
        except ValueError:
            raise HTTPException(422, "Microsoft tenant IDs must be UUIDs.") from None
        try:
            with store.transaction() as db:
                session = login_session(db, request, True)
                membership(db, org_id, session.user_id, True)
                org = db.scalar(
                    select(Organization)
                    .where(Organization.id == org_id)
                    .with_for_update()
                )
                if re.fullmatch(r"org-[a-f0-9]{32}", body.slug) and body.slug != "org-" + org_id:
                    raise HTTPException(422, "Choose a name outside the reserved org-ID format.")
                if body.slug in {"api", "auth", "static", "gallery", "health", "docs", "redoc", "openapi", "o", "a"}:
                    raise HTTPException(422, "That organization prefix is reserved.")
                alias = db.get(OrganizationAlias, body.slug)
                if alias and alias.org_id != org_id:
                    raise HTTPException(
                        409, "That organization link is already in use."
                    )
                if not alias:
                    db.add(OrganizationAlias(slug=body.slug, org_id=org_id))
                org.slug = body.slug
                org.gallery_enabled = body.gallery_enabled
                org.allowed_domains = domains
                org.microsoft_tenants = tenants
                audit(db, session.user_id, org_id, "organization.sharing", org_id)
                return org_view(org)
        except IntegrityError:
            raise HTTPException(
                409, "That organization link is already in use."
            ) from None

    @router.get("/bots/{bot_id}/sharing")
    def get_bot(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            return bot_view(db, accessible_bot(db, bot_id, session.user_id))

    @router.put("/bots/{bot_id}/sharing")
    def put_bot(bot_id: str, body: BotSharing, request: Request):
        try:
            with store.transaction() as db:
                session = login_session(db, request, True)
                bot = accessible_bot(db, bot_id, session.user_id, True)
                if re.fullmatch(r"asklit-[a-f0-9]{32}", body.slug) and body.slug != "asklit-" + bot.id:
                    raise HTTPException(422, "Choose a name outside the reserved asklit-ID format.")
                ensure_links(db, bot)
                alias = db.get(BotAlias, (bot.org_id, body.slug))
                if alias and alias.bot_id != bot.id:
                    raise HTTPException(
                        409, "That link is already in use in this organization."
                    )
                if not alias:
                    db.add(BotAlias(org_id=bot.org_id, slug=body.slug, bot_id=bot.id))
                bot.slug = body.slug
                if bot.access_mode != body.access_mode:
                    bot.access_version += 1
                    bot.access_mode = body.access_mode
                audit(db, session.user_id, bot.org_id, "bot.sharing", bot.id)
                return bot_view(db, bot)
        except IntegrityError:
            raise HTTPException(
                409, "That link is already in use in this organization."
            ) from None

    @router.get("/bots/{bot_id}/passwords")
    def passwords(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            accessible_bot(db, bot_id, session.user_id)
            return [
                {"id": k.id, "label": k.label, "enabled": k.enabled}
                for k in db.scalars(
                    select(PasswordKey)
                    .where(PasswordKey.bot_id == bot_id)
                    .order_by(PasswordKey.label, PasswordKey.id)
                )
            ]

    @router.post("/bots/{bot_id}/passwords")
    def add_password(bot_id: str, body: NewPassword, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            lookup = password_lookup(body.password, settings)
            if db.scalar(
                select(PasswordKey).where(
                    PasswordKey.bot_id == bot_id, PasswordKey.lookup == lookup
                )
            ):
                raise HTTPException(
                    409,
                    "This password already exists for this AskLit. Enable its existing entry instead.",
                )
            key = PasswordKey(
                bot_id=bot_id,
                label=body.label,
                lookup=lookup,
                password_hash=hash_password(body.password),
            )
            db.add(key)
            db.flush()
            audit(db, session.user_id, bot.org_id, "password.create", key.id)
            return {"id": key.id, "label": key.label, "enabled": key.enabled}

    @router.patch("/bots/{bot_id}/passwords/{key_id}")
    def toggle_password(
        bot_id: str, key_id: str, body: PasswordState, request: Request
    ):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            key = db.get(PasswordKey, key_id)
            if not key or key.bot_id != bot.id:
                raise HTTPException(404)
            if key.enabled != body.enabled:
                key.enabled = body.enabled
                key.version += 1
            audit(
                db,
                session.user_id,
                bot.org_id,
                "password.enable" if body.enabled else "password.disable",
                key.id,
            )
            return {"id": key.id, "label": key.label, "enabled": key.enabled}

    return router
