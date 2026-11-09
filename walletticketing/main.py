from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .db import Database
from .payments import PaymentRegistry
from .web import admin, api, shop, tickets
from .web.deps import NotAuthenticated, build_templates

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None, payments: PaymentRegistry | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, docs_url="/api/docs", redoc_url=None)

    database = Database(settings.database_url)
    database.create_all()

    app.state.settings = settings
    app.state.db = database
    app.state.payments = payments or PaymentRegistry(settings)
    app.state.templates = build_templates(settings)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="wt_session",
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(shop.router)
    app.include_router(tickets.router)
    app.include_router(admin.router)
    app.include_router(api.router)

    @app.exception_handler(NotAuthenticated)
    async def _login_redirect(request: Request, exc: NotAuthenticated):
        return RedirectResponse(f"/admin/login?next={request.url.path}", status_code=303)

    return app


def run() -> None:
    import uvicorn

    uvicorn.run("walletticketing.main:create_app", factory=True, host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
