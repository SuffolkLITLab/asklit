"""FastAPI application factory. Run: uvicorn asklit.hosted.app:create_app --factory"""

from pathlib import Path
from ipaddress import ip_address
from .security import token

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .local_accounts import local_account_router
from .auth import auth_router
from .access import safe_next
from .management import management_router
from .sharing import sharing_router
from .gallery import gallery_router
from .models import Store
from .public import public_router
from .runtime import Runtime
from .settings import Settings

ROOT = Path(__file__).parent


class FlyClientAddress:
    """Use Fly's connection address, not a client-supplied X-Forwarded-For chain.

    Enable only behind Fly ingress, binding the server to IPv4 (not private 6PN).
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw = dict(scope.get("headers", [])).get(b"fly-client-ip", b"")
            try:
                address = str(ip_address(raw.decode("ascii")))
            except (ValueError, UnicodeDecodeError):
                address = "unknown"
            scope = dict(scope, client=(address, 0))
        await self.app(scope, receive, send)


class BodyLimit:
    """Bound streamed and chunked bodies before multipart parsing."""

    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = self.limit + 1
        if length > self.limit:
            return await JSONResponse(
                {"detail": "Request is too large."}, status_code=413
            )(scope, receive, send)
        # Read with a hard bound, then replay. Uploads are capped at 10 MB.
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            messages, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > self.limit:
                    return await JSONResponse(
                        {"detail": "Request is too large."}, status_code=413
                    )(scope, receive, send)
                messages.append(message)
                if not message.get("more_body", False):
                    break

            async def replay():
                return messages.pop(0) if messages else await receive()

            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


def create_app(settings=None, store=None, runtime=None):
    settings = settings or Settings.from_env()
    store = store or Store(settings.database_url)
    runtime = runtime or Runtime(settings)
    app = FastAPI(title="AskLit Hosted", version="1.0.0", docs_url=None, redoc_url=None)
    app.state.store, app.state.settings, app.state.runtime = store, settings, runtime
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="asklit_oauth",
        https_only=settings.secure,
        same_site="lax",
        max_age=600,
    )
    app.add_middleware(BodyLimit, limit=settings.max_upload_bytes + 1024 * 1024)
    if settings.trust_fly_proxy:
        app.add_middleware(FlyClientAddress)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(auth_router(settings, store))
    app.include_router(local_account_router(settings, store))
    app.include_router(management_router(settings, store, runtime))
    app.include_router(public_router(settings, store, runtime))
    app.include_router(sharing_router(settings, store))
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if request.query_params.get("next"):
            request.session["after_login"] = safe_next(request.query_params["next"])
        if not request.session.get("local_login_csrf"):
            request.session["local_login_csrf"] = token()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "dev_mode": settings.dev_mode,
                "local_login_csrf": request.session.get("local_login_csrf", ""),
                "login_error": request.query_params.get("login_error") == "1",
                "google": bool(settings.google_client_id),
                "microsoft": bool(settings.microsoft_client_id),
            },
        )

    @app.get("/health")
    def health():
        from sqlalchemy import text

        with store.transaction() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.include_router(gallery_router(settings, store))
    return app
