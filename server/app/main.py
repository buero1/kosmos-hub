import asyncio
import logging
from base64 import b64decode
from binascii import Error as BinasciiError
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from secrets import compare_digest

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.api.routes import health, registrations, site_abilities, site_inventory, site_updates, sites, web
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.mcp_server import hub_mcp, mcp_asgi_app
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
        if settings.hub_access_enabled and not _is_public_hub_path(request.url.path):
            username = _authenticated_hub_username(request, settings)
            if username is None:
                return PlainTextResponse(
                    "Authentication required.",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="kosmos-hub"', "Cache-Control": "no-store"},
                )
            request.state.hub_access_username = username

        response = await call_next(request)
        if (
            request.url.path == "/"
            or request.url.path.startswith("/sites")
            or request.url.path == "/updates"
            or request.url.path.startswith("/update-plans")
        ):
            # Inventory and update data must not be served from a browser cache.
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(registrations.router)
    app.include_router(sites.router)
    app.include_router(site_abilities.router)
    app.include_router(site_inventory.router)
    app.include_router(site_updates.router)
    app.include_router(web.router)
    app.mount("/mcp", mcp_asgi_app)
    return app


app = create_app()


def _is_public_hub_path(path: str) -> bool:
    return path in {"/healthz", "/api/v1/registrations"}


def _authenticated_hub_username(request: Request, settings) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, encoded_credentials = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded_credentials:
        return None

    try:
        decoded_credentials = b64decode(encoded_credentials, validate=True).decode("utf-8")
    except (BinasciiError, UnicodeDecodeError):
        return None

    username, separator, password = decoded_credentials.partition(":")
    if not separator or settings.hub_access_password is None:
        return None

    expected_password = settings.hub_access_password.get_secret_value()
    if not compare_digest(username, settings.hub_access_username) or not compare_digest(password, expected_password):
        return None
    return username
