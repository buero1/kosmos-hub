from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.site import Site, SiteStatus
from app.models.site_snapshot import SiteSnapshot
from app.models.site_update_snapshot import SiteUpdateSnapshot
from app.repositories.site_repository import SiteRepository
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService


@dataclass(frozen=True)
class FleetInventoryItem:
    site: Site
    snapshot: SiteSnapshot | None
    update_snapshot: SiteUpdateSnapshot | None
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

    @property
    def core_updates(self) -> tuple[dict[str, Any], ...]:
        return self._updates_from("core_updates_json")

    @property
    def plugin_updates(self) -> tuple[dict[str, Any], ...]:
        return self._updates_from("plugin_updates_json")

    @property
    def theme_updates(self) -> tuple[dict[str, Any], ...]:
        return self._updates_from("theme_updates_json")

    @property
    def update_count(self) -> int:
        return len(self.core_updates) + len(self.plugin_updates) + len(self.theme_updates)

    def matching_update_names(self, query: str) -> list[str]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return []

        matches: list[str] = []
        for update in (*self.plugin_updates, *self.theme_updates):
            name = str(update.get("name", "")).strip()
            identifier = str(update.get("plugin_file", update.get("stylesheet", ""))).strip()
            if normalized_query in name.casefold() or normalized_query in identifier.casefold():
                matches.append(name or identifier)
        return matches

    def _updates_from(self, field_name: str) -> tuple[dict[str, Any], ...]:
        if self.update_snapshot is None:
            return ()
        updates = getattr(self.update_snapshot, field_name, [])
        return tuple(update for update in updates if isinstance(update, dict))


@dataclass(frozen=True)
class UpdateWorkbenchEntry:
    site: Site
    kind: str
    name: str
    identifier: str
    current_version: str
    target_version: str
    is_active: bool | None
    execution_ready: bool
    execution_note: str
    captured_at: datetime

    @property
    def kind_label(self) -> str:
        return {"wordpress": "WordPress", "plugin": "Plugin", "theme": "Theme"}[self.kind]

    @property
    def activity_label(self) -> str:
        if self.kind != "plugin":
            return "System"
        return "Active" if self.is_active else "Inactive"

    @property
    def review_note(self) -> str:
        if self.kind == "wordpress":
            return "Core update: not enabled"
        if self.requires_stored_crocoblock_license:
            return "Crocoblock update: the saved Hub license is activated automatically before this update."
        if self.kind == "plugin" and not self.execution_ready:
            return self.execution_note or "The update provider has not supplied an authorized package."
        if self.kind == "plugin" and self.direct_update_selectable and self.is_active:
            return "Active plugin: direct update ready"
        if self.kind == "plugin" and self.direct_update_selectable:
            return "Inactive plugin: direct update ready; it will remain inactive."
        if self.kind == "plugin":
            return self.execution_note or "The update provider has not supplied an authorized package."
        return "Theme update: not enabled"

    @property
    def requires_stored_crocoblock_license(self) -> bool:
        return self.kind == "plugin" and self.is_active is not None and not self.execution_ready and self.identifier.startswith("jet-")

    @property
    def direct_update_selectable(self) -> bool:
        return self.kind == "plugin" and self.is_active is not None and (self.execution_ready or self.requires_stored_crocoblock_license)

    @property
    def plan_key(self) -> str:
        return "|".join((str(self.site.id), self.kind, self.identifier or self.name))


class FleetInventoryService:
    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)

    def list_items(self, *, limit: int = 200) -> list[FleetInventoryItem]:
        sites = self.repository.list_sites(limit=limit)
        snapshots = self.repository.get_latest_snapshots_by_site_ids([site.id for site in sites])
        update_snapshots = self.repository.get_latest_update_snapshots_by_site_ids([site.id for site in sites])
        return [self._build_item(site, snapshots.get(site.id), update_snapshots.get(site.id)) for site in sites]

    def filter_items(
        self,
        items: list[FleetInventoryItem],
        *,
        query: str = "",
        plugin: str = "",
        status: str = "",
        inventory_state: str = "all",
        updates_state: str = "all",
        update_plugin: str = "",
        wordpress_version: str = "",
        bridge_version: str = "",
    ) -> list[FleetInventoryItem]:
        normalized_query = query.strip().casefold()
        normalized_plugin = plugin.strip().casefold()
        normalized_update_plugin = update_plugin.strip().casefold()

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
            if updates_state == "available" and item.update_count == 0:
                return False
            if updates_state == "wordpress" and not item.core_updates:
                return False
            if updates_state == "plugins" and not item.plugin_updates:
                return False
            if updates_state == "themes" and not item.theme_updates:
                return False
            if updates_state == "none" and (item.update_snapshot is None or item.update_count != 0):
                return False
            if updates_state == "missing" and item.update_snapshot is not None:
                return False
            if normalized_query and not self._matches_site_query(item.site, normalized_query):
                return False
            if normalized_plugin and not item.matching_plugin_names(normalized_plugin):
                return False
            if normalized_update_plugin and not item.matching_update_names(normalized_update_plugin):
                return False
            return True

        return [item for item in items if matches(item)]

    def summarize(self, items: list[FleetInventoryItem]) -> dict[str, int]:
        inventoried = [item for item in items if item.snapshot is not None]
        update_checked = [item for item in items if item.update_snapshot is not None]
        return {
            "total_sites": len(items),
            "inventoried_sites": len(inventoried),
            "missing_inventory_sites": len(items) - len(inventoried),
            "active_plugins": sum(item.plugin_count for item in inventoried),
            "update_checked_sites": len(update_checked),
            "missing_update_checks": len(items) - len(update_checked),
            "sites_needing_updates": sum(1 for item in update_checked if item.update_count > 0),
            "available_updates": sum(item.update_count for item in update_checked),
        }

    def build_update_workbench(self, items: list[FleetInventoryItem]) -> list[UpdateWorkbenchEntry]:
        entries: list[UpdateWorkbenchEntry] = []
        for item in items:
            if item.update_snapshot is None:
                continue

            active_plugin_files = {
                str(plugin.get("plugin_file", "")).strip()
                for plugin in item.plugins
                if str(plugin.get("plugin_file", "")).strip()
            }
            captured_at = item.update_snapshot.captured_at

            for update in item.core_updates:
                entries.append(
                    UpdateWorkbenchEntry(
                        site=item.site,
                        kind="wordpress",
                        name="WordPress core",
                        identifier=str(update.get("locale", "")).strip(),
                        current_version=str(update.get("current_version", "")).strip(),
                        target_version=str(update.get("new_version", "")).strip(),
                        is_active=None,
                        execution_ready=False,
                        execution_note="",
                        captured_at=captured_at,
                    )
                )

            for update in item.plugin_updates:
                plugin_file = str(update.get("plugin_file", "")).strip()
                entries.append(
                    UpdateWorkbenchEntry(
                        site=item.site,
                        kind="plugin",
                        name=str(update.get("name", "")).strip() or plugin_file,
                        identifier=plugin_file,
                        current_version=str(update.get("current_version", "")).strip(),
                        target_version=str(update.get("new_version", "")).strip(),
                        is_active=plugin_file in active_plugin_files,
                        execution_ready=update.get("execution_ready") is not False,
                        execution_note=str(update.get("execution_note", "")).strip(),
                        captured_at=captured_at,
                    )
                )

            for update in item.theme_updates:
                stylesheet = str(update.get("stylesheet", "")).strip()
                entries.append(
                    UpdateWorkbenchEntry(
                        site=item.site,
                        kind="theme",
                        name=str(update.get("name", "")).strip() or stylesheet,
                        identifier=stylesheet,
                        current_version=str(update.get("current_version", "")).strip(),
                        target_version=str(update.get("new_version", "")).strip(),
                        is_active=None,
                        execution_ready=False,
                        execution_note="",
                        captured_at=captured_at,
                    )
                )

        kind_order = {"wordpress": 0, "plugin": 1, "theme": 2}
        return sorted(entries, key=lambda entry: (entry.site.domain.casefold(), kind_order[entry.kind], entry.name.casefold()))

    def filter_update_workbench(
        self,
        entries: list[UpdateWorkbenchEntry],
        *,
        query: str = "",
        kind: str = "all",
        activity: str = "all",
    ) -> list[UpdateWorkbenchEntry]:
        normalized_query = query.strip().casefold()

        def matches(entry: UpdateWorkbenchEntry) -> bool:
            if kind != "all" and entry.kind != kind:
                return False
            if activity == "active" and entry.is_active is not True:
                return False
            if activity == "inactive" and entry.is_active is not False:
                return False
            if not normalized_query:
                return True

            haystack = " ".join((entry.site.domain, entry.name, entry.identifier)).casefold()
            return normalized_query in haystack

        return [entry for entry in entries if matches(entry)]

    def summarize_update_workbench(self, entries: list[UpdateWorkbenchEntry]) -> dict[str, int]:
        return {
            "total": len(entries),
            "wordpress": sum(1 for entry in entries if entry.kind == "wordpress"),
            "plugins": sum(1 for entry in entries if entry.kind == "plugin"),
            "themes": sum(1 for entry in entries if entry.kind == "theme"),
            "active_plugins": sum(1 for entry in entries if entry.kind == "plugin" and entry.is_active),
            "inactive_plugins": sum(1 for entry in entries if entry.kind == "plugin" and not entry.is_active),
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

    def refresh_verified_site_updates(self, *, limit: int = 25) -> dict[str, list[dict[str, Any]]]:
        sites = self.repository.list_sites(limit=limit)
        refreshed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for site in sites:
            if site.status != SiteStatus.verified.value:
                skipped.append({"site_id": site.id, "domain": site.domain, "reason": "site_not_verified"})
                continue
            if not self._bridge_supports_updates(site.bridge_version):
                skipped.append({"site_id": site.id, "domain": site.domain, "reason": "bridge_update_required"})
                continue

            try:
                payload = SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(site.id)
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
                    "available_updates": (
                        len(snapshot.core_updates_json)
                        + len(snapshot.plugin_updates_json)
                        + len(snapshot.theme_updates_json)
                    ),
                }
            )

        return {"refreshed": refreshed, "failed": failed, "skipped": skipped}

    def _build_item(
        self,
        site: Site,
        snapshot: SiteSnapshot | None,
        update_snapshot: SiteUpdateSnapshot | None,
    ) -> FleetInventoryItem:
        plugins_json = snapshot.plugins_json if snapshot is not None else []
        plugins = tuple(plugin for plugin in plugins_json if isinstance(plugin, dict))
        return FleetInventoryItem(site=site, snapshot=snapshot, update_snapshot=update_snapshot, plugins=plugins)

    def _matches_site_query(self, site: Site, query: str) -> bool:
        return query in site.domain.casefold() or query in site.home_url.casefold() or query in site.site_url.casefold()

    def _bridge_supports_updates(self, version: str | None) -> bool:
        if not version:
            return False
        try:
            current = tuple(int(part) for part in version.split("-", 1)[0].split("."))
        except ValueError:
            return False
        return current >= (0, 3, 7)
