from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.site import Site, SiteStatus
from app.models.site_snapshot import SiteSnapshot
from app.repositories.site_repository import SiteRepository
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError


@dataclass(frozen=True)
class FleetInventoryItem:
    site: Site
    snapshot: SiteSnapshot | None
    plugins: tuple[dict[str, Any], ...]

    @property
    def plugin_count(self) -> int:
        return len(self.plugins)

    def matching_plugin_names(self, query: str) -> list[str]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return []

        matches: list[str] = []
        for plugin in self.plugins:
            name = str(plugin.get("name", "")).strip()
            plugin_file = str(plugin.get("plugin_file", "")).strip()
            if normalized_query in name.casefold() or normalized_query in plugin_file.casefold():
                matches.append(name or plugin_file)
        return matches


class FleetInventoryService:
    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)

    def list_items(self, *, limit: int = 200) -> list[FleetInventoryItem]:
        sites = self.repository.list_sites(limit=limit)
        snapshots = self.repository.get_latest_snapshots_by_site_ids([site.id for site in sites])
        return [self._build_item(site, snapshots.get(site.id)) for site in sites]

    def filter_items(
        self,
        items: list[FleetInventoryItem],
        *,
        query: str = "",
        plugin: str = "",
        status: str = "",
        inventory_state: str = "all",
        wordpress_version: str = "",
        bridge_version: str = "",
    ) -> list[FleetInventoryItem]:
        normalized_query = query.strip().casefold()
        normalized_plugin = plugin.strip().casefold()

        def matches(item: FleetInventoryItem) -> bool:
            if status and item.site.status != status:
                return False
            if wordpress_version and item.site.wordpress_version != wordpress_version:
                return False
            if bridge_version and item.site.bridge_version != bridge_version:
                return False
            if inventory_state == "present" and item.snapshot is None:
                return False
            if inventory_state == "missing" and item.snapshot is not None:
                return False
            if normalized_query and not self._matches_site_query(item.site, normalized_query):
                return False
            if normalized_plugin and not item.matching_plugin_names(normalized_plugin):
                return False
            return True

        return [item for item in items if matches(item)]

    def summarize(self, items: list[FleetInventoryItem]) -> dict[str, int]:
        inventoried = [item for item in items if item.snapshot is not None]
        return {
            "total_sites": len(items),
            "inventoried_sites": len(inventoried),
            "missing_inventory_sites": len(items) - len(inventoried),
            "active_plugins": sum(item.plugin_count for item in inventoried),
        }

    def refresh_verified_site_states(self, *, limit: int = 25) -> dict[str, list[dict[str, Any]]]:
        sites = self.repository.list_sites(limit=limit)
        refreshed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for site in sites:
            if site.status != SiteStatus.verified.value:
                skipped.append({"site_id": site.id, "domain": site.domain, "reason": "site_not_verified"})
                continue

            try:
                payload = SiteInventoryService(db=self.db, cipher=self.cipher).refresh_site_state(site.id)
            except SiteMcpProxyError as exc:
                failed.append(
                    {
                        "site_id": site.id,
                        "domain": site.domain,
                        "code": exc.code,
                        "message": exc.message,
                    }
                )
                continue

            snapshot = payload["snapshot"]
            refreshed.append(
                {
                    "site_id": site.id,
                    "domain": site.domain,
                    "captured_at": snapshot.captured_at,
                    "active_plugin_count": len(snapshot.plugins_json),
                }
            )

        return {"refreshed": refreshed, "failed": failed, "skipped": skipped}

    def _build_item(self, site: Site, snapshot: SiteSnapshot | None) -> FleetInventoryItem:
        plugins_json = snapshot.plugins_json if snapshot is not None else []
        plugins = tuple(plugin for plugin in plugins_json if isinstance(plugin, dict))
        return FleetInventoryItem(site=site, snapshot=snapshot, plugins=plugins)

    def _matches_site_query(self, site: Site, query: str) -> bool:
        return query in site.domain.casefold() or query in site.home_url.casefold() or query in site.site_url.casefold()
