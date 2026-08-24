from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api.routes import health, registrations, site_abilities, site_inventory, sites, web
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import engine
from app.mcp_server import hub_mcp, mcp_asgi_app


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    async with AsyncExitStack() as stack:
        if settings.auto_create_tables:
            Base.metadata.create_all(bind=engine)
        await stack.enter_async_context(hub_mcp.session_manager.run())
        yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(registrations.router)
    app.include_router(sites.router)
    app.include_router(site_abilities.router)
    app.include_router(site_inventory.router)
    app.include_router(web.router)
    app.mount("/mcp", mcp_asgi_app)
    return app


app = create_app()
