from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.repositories.site_repository import SiteRepository
from app.schemas.inventory import (
    SiteCapabilityInventoryResponse,
    SiteInventoryRefreshResponse,
    SiteStateRefreshResponse,
    SiteStateSnapshotResponse,
    StoredSiteCapabilityResponse,
)
from app.schemas.site import SiteDetailResponse
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService

hub_mcp = MCPServer(
    "kosmos-hub",
)

mcp_asgi_app = hub_mcp.streamable_http_app(
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "localhost:*",
            "127.0.0.1:*",
            "kosmos-hub.31-70-92-95.sslip.io:*",
        ],
        allowed_origins=[
            "http://localhost:*",
            "http://127.0.0.1:*",
            "https://kosmos-hub.31-70-92-95.sslip.io",
        ],
    ),
)


def _proxy_error_payload(exc: SiteMcpProxyError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "status_code": exc.status_code,
        },
    }


@hub_mcp.tool()
def search_sites(query: str = "") -> dict[str, Any]:
    """Search registered sites by domain or URL."""
    with SessionLocal() as db:
        repository = SiteRepository(db)
        sites = repository.list_sites(limit=200)
        normalized_query = query.strip().lower()
        if normalized_query:
            sites = [
                site
                for site in sites
                if normalized_query in site.domain.lower()
                or normalized_query in site.home_url.lower()
                or normalized_query in site.site_url.lower()
            ]
        return {
            "ok": True,
            "payload": {
                "items": [SiteDetailResponse.model_validate(site).model_dump(mode="json") for site in sites],
            },
        }


@hub_mcp.tool()
def get_site(site_id: int) -> dict[str, Any]:
    """Get one registered site with its stored connection information."""
    with SessionLocal() as db:
        repository = SiteRepository(db)
        site = repository.get_site(site_id)
        if site is None:
            return _proxy_error_payload(SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404))
        return {
            "ok": True,
            "payload": SiteDetailResponse.model_validate(site).model_dump(mode="json"),
        }


@hub_mcp.tool()
def discover_site_capabilities(site_id: int) -> dict[str, Any]:
    """Discover public abilities exposed by one WordPress site through Kosmos Bridge."""
    with SessionLocal() as db:
        service = SiteMcpProxyService(db=db, cipher=get_secret_cipher())
        try:
            payload = service.discover_abilities(site_id)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        return {"ok": True, "payload": payload}


@hub_mcp.tool()
def get_site_ability_info(site_id: int, ability_name: str) -> dict[str, Any]:
    """Fetch schema and metadata for one site ability."""
    with SessionLocal() as db:
        service = SiteMcpProxyService(db=db, cipher=get_secret_cipher())
        try:
            payload = service.get_ability_info(site_id, ability_name)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        return {"ok": True, "payload": payload}


@hub_mcp.tool()
def execute_site_capability(site_id: int, ability_name: str, ability_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute one site ability via the registered Kosmos Bridge endpoint."""
    with SessionLocal() as db:
        service = SiteMcpProxyService(db=db, cipher=get_secret_cipher())
        try:
            payload = service.execute_ability(site_id, ability_name, ability_input)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        return {"ok": True, "payload": payload}


@hub_mcp.tool()
def get_site_inventory(site_id: int) -> dict[str, Any]:
    """Read the last stored capability inventory for one site from kosmos-hub."""
    with SessionLocal() as db:
        service = SiteInventoryService(db=db, cipher=get_secret_cipher())
        try:
            items = service.list_site_capabilities(site_id)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        payload = SiteCapabilityInventoryResponse(
            items=[StoredSiteCapabilityResponse.model_validate(item) for item in items]
        ).model_dump(mode="json")
        return {"ok": True, "payload": payload}


@hub_mcp.tool()
def refresh_site_inventory(site_id: int) -> dict[str, Any]:
    """Refresh and store the capability inventory for one WordPress site."""
    with SessionLocal() as db:
        service = SiteInventoryService(db=db, cipher=get_secret_cipher())
        try:
            payload = service.refresh_site_inventory(site_id)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        response = SiteInventoryRefreshResponse(
            site_id=payload["site_id"],
            provider=payload["provider"],
            refreshed_at=payload["refreshed_at"],
            items=[StoredSiteCapabilityResponse.model_validate(item) for item in payload["items"]],
        ).model_dump(mode="json")
        return {"ok": True, "payload": response}


@hub_mcp.tool()
def get_site_state_snapshot(site_id: int) -> dict[str, Any]:
    """Read the last stored site state snapshot for one site from kosmos-hub."""
    with SessionLocal() as db:
        service = SiteInventoryService(db=db, cipher=get_secret_cipher())
        try:
            snapshot = service.get_latest_site_snapshot(site_id)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        return {
            "ok": True,
            "payload": SiteStateSnapshotResponse.model_validate(snapshot).model_dump(mode="json") if snapshot else None,
        }


@hub_mcp.tool()
def refresh_site_state_snapshot(site_id: int) -> dict[str, Any]:
    """Refresh environment and active plugin state for one WordPress site and store it as a snapshot."""
    with SessionLocal() as db:
        service = SiteInventoryService(db=db, cipher=get_secret_cipher())
        try:
            payload = service.refresh_site_state(site_id)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        response = SiteStateRefreshResponse(
            site_id=payload["site_id"],
            refreshed_at=payload["refreshed_at"],
            snapshot=SiteStateSnapshotResponse.model_validate(payload["snapshot"]),
        ).model_dump(mode="json")
        return {"ok": True, "payload": response}
