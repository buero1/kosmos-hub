import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import inspect, text
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import accounts, assistant, health, registrations, site_abilities, site_backups, site_inventory, site_updates, sites, web
from app.core.config import get_settings
from app.core.mcp_context import reset_mcp_actor, set_mcp_actor
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.mcp_server import hub_mcp, mcp_asgi_app
from app.services.hub_accounts import HubAccountService
from app.services.fleet_refresh import FleetRefreshService
from app.services.maintenance_runs import MaintenanceRunService
from app.services.maintenance_worker import process_pending_direct_updates

logger = logging.getLogger(__name__)


def _ensure_phase_one_schema() -> None:
    """Apply the small additive schema changes used before Alembic is introduced."""
    inspector = inspect(engine)
    if "fleet_refresh_settings" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("fleet_refresh_settings")}
    if "max_parallel_direct_updates" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE fleet_refresh_settings "
                "ADD COLUMN max_parallel_direct_updates INT NOT NULL DEFAULT 5 "
                "AFTER max_parallel_site_checks"
            )
        )
    logger.info("Added fleet_refresh_settings.max_parallel_direct_updates with default 5.")


def _queue_scheduled_fleet_refresh() -> int | None:
    return FleetRefreshService.queue_scheduled_run()


async def _fleet_update_refresh_loop(initial_delay_seconds: int, interval_hours: int) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            run_id = await asyncio.to_thread(_queue_scheduled_fleet_refresh)
            if run_id is not None:
                logger.info("Scheduled fleet status refresh run %s.", run_id)
        except Exception:
            logger.exception("Fleet update refresh scheduling failed unexpectedly.")
        await asyncio.sleep(interval_hours * 60 * 60)


async def _fleet_refresh_worker_loop(initial_delay_seconds: int, interval_seconds: int) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            run_id = await asyncio.to_thread(FleetRefreshService.process_next_queued_run)
            if run_id is not None:
                logger.info("Fleet status refresh run %s completed in the background.", run_id)
        except Exception:
            logger.exception("Fleet refresh worker failed unexpectedly.")
        await asyncio.sleep(interval_seconds)


def _poll_maintenance_runs() -> dict[str, int]:
    with SessionLocal() as db:
        service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
        backup_result = service.poll_active_updraftplus_backups(limit=25)
    plugin_update_result = process_pending_direct_updates()
    return {
        "checked": backup_result["checked"] + plugin_update_result["checked"],
        "succeeded": backup_result["succeeded"] + plugin_update_result["succeeded"],
        "failed": backup_result["failed"] + plugin_update_result["failed"],
        "waiting": backup_result["waiting"] + plugin_update_result["waiting"],
        "skipped": plugin_update_result["skipped"],
    }


async def _maintenance_run_poll_loop(initial_delay_seconds: int, interval_seconds: int) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            result = await asyncio.to_thread(_poll_maintenance_runs)
            if result["checked"]:
                logger.info("Maintenance polling: %s", result)
        except Exception:
            logger.exception("Maintenance backup polling failed unexpectedly.")
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    async with AsyncExitStack() as stack:
        if settings.auto_create_tables:
            Base.metadata.create_all(bind=engine)
        _ensure_phase_one_schema()
        recovered_runs = await asyncio.to_thread(FleetRefreshService.recover_interrupted_runs)
        if recovered_runs:
            logger.info("Re-queued %s interrupted fleet refresh run(s).", recovered_runs)
        await stack.enter_async_context(hub_mcp.session_manager.run())
        fleet_worker_task = asyncio.create_task(
            _fleet_refresh_worker_loop(
                settings.fleet_refresh_worker_initial_delay_seconds,
                settings.fleet_refresh_worker_poll_interval_seconds,
            )
        )
        refresh_task = None
        if settings.fleet_updates_auto_refresh:
            refresh_task = asyncio.create_task(
                _fleet_update_refresh_loop(
                    settings.fleet_updates_initial_delay_seconds,
                    settings.fleet_updates_refresh_interval_hours,
                )
            )
        maintenance_task = None
        if settings.maintenance_runs_auto_poll:
            maintenance_task = asyncio.create_task(
                _maintenance_run_poll_loop(
                    settings.maintenance_runs_initial_delay_seconds,
                    settings.maintenance_runs_poll_interval_seconds,
                )
            )
        try:
            yield
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task
            fleet_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await fleet_worker_task
            if maintenance_task is not None:
                maintenance_task.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance_task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.middleware("http")
    async def protect_hub_and_prevent_stale_web_pages(request: Request, call_next):
        mcp_context_token = None
        if _is_mcp_path(request.url.path):
            mcp_actor = _authenticated_mcp_actor(request)
            if mcp_actor is None:
                user = _authenticated_hub_user(request)
                if user is None:
                    return PlainTextResponse(
                        "MCP bearer token or authenticated Hub session required.",
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                    )
                mcp_actor = f"mcp-session:{user.username[:48]}"
                request.state.hub_user = user
            request.state.mcp_actor = mcp_actor
            mcp_context_token = set_mcp_actor(mcp_actor)
        elif not _is_public_hub_path(request.url.path):
            user = _authenticated_hub_user(request)
            if user is None:
                if request.method == "GET" and _prefers_html(request):
                    next_url = request.url.path
                    if request.url.query:
                        next_url = f"{next_url}?{request.url.query}"
                    return RedirectResponse(url=f"/account/login?{urlencode({'next': next_url})}", status_code=303)
                return PlainTextResponse("Authentication required.", status_code=401, headers={"Cache-Control": "no-store"})
            request.state.hub_user = user

        try:
            response = await call_next(request)
        finally:
            if mcp_context_token is not None:
                reset_mcp_actor(mcp_context_token)
        if (
            request.url.path == "/"
            or request.url.path.startswith("/account")
            or request.url.path.startswith("/sites")
            or request.url.path == "/updates"
            or request.url.path.startswith("/update-plans")
            or request.url.path.startswith("/assistant")
        ):
            # Inventory and update data must not be served from a browser cache.
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(registrations.router)
    app.include_router(accounts.router)
    app.include_router(accounts.bootstrap_router)
    app.include_router(assistant.router)
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


def _is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _authenticated_mcp_actor(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    with SessionLocal() as db:
        service = HubAccountService(db=db, app_secret_key=get_settings().app_secret_key)
        authenticated = service.authenticate_mcp_access_token(token)
        if authenticated is None:
            return None
        user, access_token = authenticated
        return f"mcp:{user.username[:48]}:{access_token.id}"


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
