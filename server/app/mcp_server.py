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
from app.schemas.updates import SiteUpdateRefreshResponse, SiteUpdateSnapshotResponse
from app.schemas.site import SiteDetailResponse
from app.services.fleet_inventory import FleetInventoryService
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService
from app.services.site_updates import SiteUpdateService

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


@hub_mcp.tool()
def get_site_update_snapshot(site_id: int) -> dict[str, Any]:
    """Read the last stored WordPress, plugin, and theme update state for one site."""
    with SessionLocal() as db:
        service = SiteUpdateService(db=db, cipher=get_secret_cipher())
        try:
            snapshot = service.get_latest_site_update_snapshot(site_id)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        return {
            "ok": True,
            "payload": SiteUpdateSnapshotResponse.model_validate(snapshot).model_dump(mode="json") if snapshot else None,
        }


@hub_mcp.tool()
def refresh_site_update_snapshot(site_id: int) -> dict[str, Any]:
    """Read and store available updates for one WordPress site without installing anything."""
    with SessionLocal() as db:
        service = SiteUpdateService(db=db, cipher=get_secret_cipher())
        try:
            payload = service.refresh_site_updates(site_id)
        except SiteMcpProxyError as exc:
            return _proxy_error_payload(exc)
        response = SiteUpdateRefreshResponse(
            site_id=payload["site_id"],
            refreshed_at=payload["refreshed_at"],
            snapshot=SiteUpdateSnapshotResponse.model_validate(payload["snapshot"]),
        ).model_dump(mode="json")
        return {"ok": True, "payload": response}


@hub_mcp.tool()
def search_site_inventory(
    query: str = "",
    plugin: str = "",
    wordpress_version: str = "",
    bridge_version: str = "",
    inventory_state: str = "all",
    updates_state: str = "all",
    update_plugin: str = "",
) -> dict[str, Any]:
    """Find sites by stored WordPress, Bridge, and active-plugin inventory data."""
    if inventory_state not in {"all", "present", "missing"}:
        return _proxy_error_payload(
            SiteMcpProxyError(
                "INVALID_INVENTORY_STATE",
                "inventory_state must be all, present, or missing.",
                status_code=422,
            )
        )
    if updates_state not in {"all", "available", "wordpress", "plugins", "themes", "none", "missing"}:
        return _proxy_error_payload(
            SiteMcpProxyError(
                "INVALID_UPDATES_STATE",
                "updates_state must be all, available, wordpress, plugins, themes, none, or missing.",
                status_code=422,
            )
        )

    with SessionLocal() as db:
        service = FleetInventoryService(db=db, cipher=get_secret_cipher())
        items = service.filter_items(
            service.list_items(limit=200),
            query=query,
            plugin=plugin,
            inventory_state=inventory_state,
            updates_state=updates_state,
            update_plugin=update_plugin,
            wordpress_version=wordpress_version,
            bridge_version=bridge_version,
        )
        return {
            "ok": True,
            "payload": {
                "items": [
                    {
                        "site_id": item.site.id,
                        "domain": item.site.domain,
                        "status": item.site.status,
                        "wordpress_version": item.site.wordpress_version,
                        "php_version": item.site.php_version,
                        "bridge_version": item.site.bridge_version,
                        "inventory_captured_at": item.snapshot.captured_at if item.snapshot else None,
                        "active_plugin_count": item.plugin_count,
                        "updates_captured_at": item.update_snapshot.captured_at if item.update_snapshot else None,
                        "available_update_count": item.update_count,
                        "core_updates": list(item.core_updates),
                        "plugin_updates": list(item.plugin_updates),
                        "theme_updates": list(item.theme_updates),
                        "plugins": [
                            {
                                "name": plugin.get("name"),
                                "plugin_file": plugin.get("plugin_file"),
                                "version": plugin.get("version"),
                            }
                            for plugin in item.plugins
                        ],
                    }
                    for item in items
                ],
                "summary": service.summarize(items),
            },
        }


@hub_mcp.tool()
def refresh_verified_site_inventories(limit: int = 25) -> dict[str, Any]:
    """Read and store environment and active-plugin snapshots for verified WordPress sites."""
    if limit < 1 or limit > 100:
        return _proxy_error_payload(
            SiteMcpProxyError("INVALID_LIMIT", "limit must be between 1 and 100.", status_code=422)
        )

    with SessionLocal() as db:
        service = FleetInventoryService(db=db, cipher=get_secret_cipher())
        return {"ok": True, "payload": service.refresh_verified_site_states(limit=limit)}


@hub_mcp.tool()
def refresh_verified_site_updates(limit: int = 25) -> dict[str, Any]:
    """Read and store available updates for every compatible verified WordPress site."""
    if limit < 1 or limit > 100:
        return _proxy_error_payload(
            SiteMcpProxyError("INVALID_LIMIT", "limit must be between 1 and 100.", status_code=422)
        )

    with SessionLocal() as db:
        service = FleetInventoryService(db=db, cipher=get_secret_cipher())
        return {"ok": True, "payload": service.refresh_verified_site_updates(limit=limit)}
