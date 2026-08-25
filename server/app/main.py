import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import accounts, health, registrations, site_abilities, site_backups, site_inventory, site_updates, sites, web
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.mcp_server import hub_mcp, mcp_asgi_app
from app.services.hub_accounts import HubAccountService
from app.services.fleet_inventory import FleetInventoryService
from app.core.security import get_secret_cipher

logger = logging.getLogger(__name__)


def _refresh_fleet_updates() -> dict[str, list[dict[str, object]]]:
    with SessionLocal() as db:
        service = FleetInventoryService(db=db, cipher=get_secret_cipher())
        return service.refresh_verified_site_updates(limit=100)


async def _fleet_update_refresh_loop(initial_delay_seconds: int, interval_hours: int) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            result = await asyncio.to_thread(_refresh_fleet_updates)
            logger.info(
                "Fleet update refresh completed: %s refreshed, %s failed, %s skipped.",
                len(result["refreshed"]),
                len(result["failed"]),
                len(result["skipped"]),
            )
        except Exception:
            logger.exception("Fleet update refresh failed unexpectedly.")
        await asyncio.sleep(interval_hours * 60 * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    async with AsyncExitStack() as stack:
        if settings.auto_create_tables:
            Base.metadata.create_all(bind=engine)
        await stack.enter_async_context(hub_mcp.session_manager.run())
        refresh_task = None
        if settings.fleet_updates_auto_refresh:
            refresh_task = asyncio.create_task(
                _fleet_update_refresh_loop(
                    settings.fleet_updates_initial_delay_seconds,
                    settings.fleet_updates_refresh_interval_hours,
                )
            )
        try:
            yield
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.middleware("http")
    async def protect_hub_and_prevent_stale_web_pages(request: Request, call_next):
        if not _is_public_hub_path(request.url.path):
            user = _authenticated_hub_user(request)
            if user is None:
                if request.method == "GET" and _prefers_html(request):
                    next_url = request.url.path
                    if request.url.query:
                        next_url = f"{next_url}?{request.url.query}"
                    return RedirectResponse(url=f"/account/login?{urlencode({'next': next_url})}", status_code=303)
                return PlainTextResponse("Authentication required.", status_code=401, headers={"Cache-Control": "no-store"})
            request.state.hub_user = user

        response = await call_next(request)
        if (
            request.url.path == "/"
            or request.url.path.startswith("/account")
            or request.url.path.startswith("/sites")
            or request.url.path == "/updates"
            or request.url.path.startswith("/update-plans")
        ):
            # Inventory and update data must not be served from a browser cache.
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(registrations.router)
    app.include_router(accounts.router)
    app.include_router(accounts.bootstrap_router)
    app.include_router(sites.router)
    app.include_router(site_abilities.router)
    app.include_router(site_backups.router)
    app.include_router(site_inventory.router)
    app.include_router(site_updates.router)
    app.include_router(web.router)
    app.mount("/mcp", mcp_asgi_app)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        session_cookie="kosmos_hub_session",
        max_age=12 * 60 * 60,
        same_site="lax",
        https_only=settings.public_base_url.startswith("https://"),
    )
    return app


app = create_app()


def _is_public_hub_path(path: str) -> bool:
    return path in {"/healthz", "/api/v1/registrations", "/account/login", "/account/setup", "/internal/bootstrap-token"}


def _authenticated_hub_user(request: Request):
    user_id = request.session.get("user_id")
    session_version = request.session.get("session_version")
    if not isinstance(user_id, int) or not isinstance(session_version, int):
        return None
    with SessionLocal() as db:
        service = HubAccountService(db=db, app_secret_key=get_settings().app_secret_key)
        user = service.get_user(user_id)
        if user is None or not user.is_active or user.session_version != session_version:
            return None
        db.expunge(user)
        return user


def _prefers_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")
