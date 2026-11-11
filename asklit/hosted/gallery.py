"""Organization galleries and stable, read-only public entry pages."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from .access import matches_organization, public_path
from .models import Bot, BotAlias, Organization, OrganizationAlias, Revision
from .public import public_bot
from .security import login_session


def resolve_org(db, slug):
    alias = db.get(OrganizationAlias, slug)
    if not alias:
        raise HTTPException(404, "Organization not found.")
    return db.get(Organization, alias.org_id)


def resolve_bot(db, org_slug, slug):
    org = resolve_org(db, org_slug)
    alias = db.get(BotAlias, (org.id, slug))
    if not alias:
        raise HTTPException(404, "AskLit not found.")
    return public_bot(db, alias.bot_id)


def gallery_router(settings, store):
    router = APIRouter()
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

    def gallery_data(db, request, organization, q, page):
        org = resolve_org(db, organization) if organization else None
        if org and not org.gallery_enabled:
            raise HTTPException(404, "This gallery is not enabled.")
        member = False
        if org:
            try:
                session = login_session(db, request)
                member = matches_organization(db, org, session.user_id, settings)
            except HTTPException:
                pass
        statement = (
            select(Bot, Revision, Organization)
            .join(Revision, Revision.id == Bot.published_id)
            .join(Organization, Organization.id == Bot.org_id)
            .where(Bot.archived.is_(False), Organization.gallery_enabled.is_(True))
        )
        statement = statement.where(
            or_(Bot.access_mode == "public", Bot.access_mode == "organization")
            if member
            else Bot.access_mode == "public"
        )
        if org:
            statement = statement.where(Bot.org_id == org.id)
        if q:
            statement = statement.where(
                Revision.config["title"]
                .as_string()
                .ilike(
                    "%" + q.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"
                )
            )
        count = db.scalar(select(func.count()).select_from(statement.subquery()))
        rows = db.execute(
            statement.order_by(Bot.id).offset((page - 1) * 24).limit(24)
        ).all()
        return {
            "organization": {"name": org.name, "slug": org.slug} if org else None,
            "items": [
                {
                    "id": b.id,
                    "title": r.config["title"],
                    "description": r.config.get("description", ""),
                    "organization": o.name,
                    "organization_slug": o.slug,
                    "slug": b.slug,
                    "access_mode": b.access_mode,
                    "url": settings.base_url + public_path(db, b),
                }
                for b, r, o in rows
            ],
            "page": page,
            "has_more": page * 24 < count,
            "query": q,
            "member": member,
        }

    @router.get("/api/public/gallery")
    def api_gallery(
        request: Request,
        organization: str = "",
        q: str = Query(default="", max_length=100),
        page: int = Query(default=1, ge=1, le=10000),
    ):
        with store.transaction() as db:
            return gallery_data(db, request, organization, q, page)

    @router.get("/api/public/organizations/{org_slug}/bots/{slug}")
    def resolve(org_slug: str, slug: str):
        with store.transaction() as db:
            bot = resolve_bot(db, org_slug, slug)
            return {"id": bot.id, "url": settings.base_url + public_path(db, bot)}

    def render_gallery(request, org_slug, q, page):
        with store.transaction() as db:
            data = gallery_data(db, request, org_slug, q, page)
            canonical = (
                "/" + data["organization"]["slug"]
                if data["organization"]
                else "/gallery"
            )
            if org_slug and org_slug != data["organization"]["slug"]:
                return RedirectResponse(
                    canonical + "?" + urlencode({"q": q, "page": page}), status_code=308
                )
        return templates.TemplateResponse(
            request=request,
            name="gallery.html",
            context={
                **data,
                "canonical": canonical,
                "signin": "/?" + urlencode({"next": canonical}),
            },
        )

    @router.get("/gallery")
    def gallery(
        request: Request,
        q: str = Query(default="", max_length=100),
        page: int = Query(default=1, ge=1, le=10000),
    ):
        return render_gallery(request, "", q, page)

    @router.get("/{org_slug}")
    @router.get("/o/{org_slug}")
    def org_gallery(
        org_slug: str,
        request: Request,
        q: str = Query(default="", max_length=100),
        page: int = Query(default=1, ge=1, le=10000),
    ):
        return render_gallery(request, org_slug, q, page)

    def render_chat(request, bot_id):
        with store.transaction() as db:
            bot = public_bot(db, bot_id)
            canonical = public_path(db, bot)
            # Only configured frontend origins can receive a sign-in handoff.
            opener = request.query_params.get("opener_origin", "")
            if opener not in settings.origins:
                opener = ""
            if request.url.path != canonical:
                return RedirectResponse(
                    canonical
                    + ("?" + urlencode({"opener_origin": opener}) if opener else ""),
                    status_code=308,
                )
            destination = canonical + (
                "?" + urlencode({"opener_origin": opener}) if opener else ""
            )
            return templates.TemplateResponse(
                request=request,
                name="public_chat.html",
                context={
                    "bot_id": bot.id,
                    "opener": opener,
                    "signin": "/?" + urlencode({"next": destination}),
                },
            )

    @router.get("/a/{bot_id}")
    def by_id(bot_id: str, request: Request):
        return render_chat(request, bot_id)

    @router.get("/{org_slug}/{slug}")
    @router.get("/o/{org_slug}/{slug}")
    def chat(org_slug: str, slug: str, request: Request):
        with store.transaction() as db:
            bot_id = resolve_bot(db, org_slug, slug).id
        return render_chat(request, bot_id)

    return router
