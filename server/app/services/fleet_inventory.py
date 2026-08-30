from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.site import Site, SiteStatus
from app.models.site_snapshot import SiteSnapshot
from app.models.site_update_snapshot import SiteUpdateSnapshot
from app.repositories.site_repository import SiteRepository
from app.services.site_inventory import SiteInventoryService
from app.services.official_plugin_versions import OfficialPluginVersionService
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService


@dataclass(frozen=True)
class FleetInventoryItem:
    site: Site
    snapshot: SiteSnapshot | None
    update_snapshot: SiteUpdateSnapshot | None
    plugins: tuple[dict[str, Any], ...]
    ability_names: frozenset[str] = frozenset()

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

    def supports_ability(self, ability_name: str) -> bool:
        return ability_name in self.ability_names

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
    update_available: bool
    update_checked: bool
    execution_ready: bool
    execution_note: str
    captured_at: datetime
    fleet_observed_version: str = ""
    fleet_observed_site_count: int = 0
    official_version: str = ""
    official_source: str = ""
    official_checked_at: datetime | None = None
    official_mismatch: bool = False
    official_note: str = "Official version not checked yet."
    diagnosis_status: str = "not-checked"
    diagnosis_label: str = "Not checked"
    diagnosis_note: str = "Run the official version check to diagnose this plugin."

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
        if self.kind == "wordpress" and self.direct_update_selectable:
            return "WordPress core: direct update ready"
        if self.kind == "wordpress":
            return self.execution_note or "WordPress core update is not ready."
        if self.kind == "plugin" and not self.update_available:
            return "No update is currently available." if self.update_checked else "Plugin inventory is available, but no update check has been recorded yet."
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
        if self.kind == "theme" and self.direct_update_selectable:
            return "Theme: direct update ready"
        return self.execution_note or "Theme update is not ready."

    @property
    def requires_stored_crocoblock_license(self) -> bool:
        return self.kind == "plugin" and self.update_available and self.is_active is not None and not self.execution_ready and self.identifier.startswith("jet-")

    @property
    def direct_update_selectable(self) -> bool:
        if not self.update_available or not self.current_version or not self.target_version:
            return False
        if self.kind == "plugin":
            return self.is_active is not None and (self.execution_ready or self.requires_stored_crocoblock_license)
        return self.kind in {"wordpress", "theme"} and self.execution_ready

    @property
    def plan_key(self) -> str:
        return "|".join((str(self.site.id), self.kind, self.identifier or self.name))


class FleetInventoryService:
    KOSMOS_BRIDGE_PLUGIN_FILE = OfficialPluginVersionService.KOSMOS_BRIDGE_PLUGIN_FILE
    THEME_UPDATE_ABILITY = "kosmos-bridge/update-theme"
    WORDPRESS_CORE_UPDATE_ABILITY = "kosmos-bridge/update-wordpress-core"

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)

    def list_items(self, *, limit: int = 1000) -> list[FleetInventoryItem]:
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
        customer_status: str = "",
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
            if customer_status == "unlinked" and item.site.customer is not None:
                return False
            if customer_status and customer_status != "unlinked":
                customer = item.site.customer
                if customer is None or customer.zoho_status != customer_status:
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
            "active_plugins": sum(
                1
                for item in inventoried
                for plugin in item.plugins
                if plugin.get("active") is not False
            ),
            "installed_plugins": sum(item.plugin_count for item in inventoried),
            "update_checked_sites": len(update_checked),
            "missing_update_checks": len(items) - len(update_checked),
            "sites_needing_updates": sum(1 for item in update_checked if item.update_count > 0),
            "available_updates": sum(item.update_count for item in update_checked),
        }

    def build_update_workbench(self, items: list[FleetInventoryItem]) -> list[UpdateWorkbenchEntry]:
        entries: list[UpdateWorkbenchEntry] = []
        for item in items:
            if item.snapshot is None and item.update_snapshot is None:
                continue

            update_checked = item.update_snapshot is not None
            captured_at = item.update_snapshot.captured_at if item.update_snapshot is not None else item.snapshot.captured_at
            plugin_updates_by_file = {
                str(update.get("plugin_file", "")).strip(): update
                for update in item.plugin_updates
                if str(update.get("plugin_file", "")).strip()
            }

            for plugin in item.plugins:
                plugin_file = str(plugin.get("plugin_file", "")).strip()
                if not plugin_file:
                    continue
                update = plugin_updates_by_file.pop(plugin_file, None)
                target_version = str(update.get("new_version", "")).strip() if update else ""
                update_available = bool(target_version)
                active = plugin.get("active")
                entries.append(
                    UpdateWorkbenchEntry(
                        site=item.site,
                        kind="plugin",
                        name=str(plugin.get("name", "")).strip() or plugin_file,
                        identifier=plugin_file,
                        current_version=str(plugin.get("version", plugin.get("current_version", ""))).strip(),
                        target_version=target_version,
                        is_active=active if isinstance(active, bool) else True,
                        update_available=update_available,
                        update_checked=update_checked,
                        execution_ready=update.get("execution_ready") is not False if update_available else False,
                        execution_note=str(update.get("execution_note", "")).strip() if update else "",
                        captured_at=captured_at,
                    )
                )

            for update in item.core_updates:
                execution_ready = item.supports_ability(self.WORDPRESS_CORE_UPDATE_ABILITY)
                entries.append(
                    UpdateWorkbenchEntry(
                        site=item.site,
                        kind="wordpress",
                        name="WordPress core",
                        identifier="wordpress-core",
                        current_version=str(update.get("current_version", "")).strip(),
                        target_version=str(update.get("new_version", "")).strip(),
                        is_active=None,
                        update_available=True,
                        update_checked=True,
                        execution_ready=execution_ready,
                        execution_note=(
                            "Update this site to Kosmos Bridge 0.3.48 or newer, then refresh its status."
                            if not execution_ready
                            else ""
                        ),
                        captured_at=captured_at,
                    )
                )

            for plugin_file, update in plugin_updates_by_file.items():
                entries.append(
                    UpdateWorkbenchEntry(
                        site=item.site,
                        kind="plugin",
                        name=str(update.get("name", "")).strip() or plugin_file,
                        identifier=plugin_file,
                        current_version=str(update.get("current_version", "")).strip(),
                        target_version=str(update.get("new_version", "")).strip(),
                        is_active=None,
                        update_available=True,
                        update_checked=True,
                        execution_ready=update.get("execution_ready") is not False,
                        execution_note=str(update.get("execution_note", "")).strip(),
                        captured_at=captured_at,
                    )
                )

            for update in item.theme_updates:
                stylesheet = str(update.get("stylesheet", "")).strip()
                execution_ready = item.supports_ability(self.THEME_UPDATE_ABILITY)
                entries.append(
                    UpdateWorkbenchEntry(
                        site=item.site,
                        kind="theme",
                        name=str(update.get("name", "")).strip() or stylesheet,
                        identifier=stylesheet,
                        current_version=str(update.get("current_version", "")).strip(),
                        target_version=str(update.get("new_version", "")).strip(),
                        is_active=None,
                        update_available=True,
                        update_checked=True,
                        execution_ready=execution_ready,
                        execution_note=(
                            "Update this site to Kosmos Bridge 0.3.48 or newer, then refresh its status."
                            if not execution_ready
                            else ""
                        ),
                        captured_at=captured_at,
                    )
                )

        entries = self._attach_fleet_observed_versions(entries)
        entries = self._attach_official_plugin_versions(entries)
        entries = self._offer_kosmos_bridge_updates(entries)
        kind_order = {"wordpress": 0, "plugin": 1, "theme": 2}
        return sorted(entries, key=lambda entry: (entry.site.domain.casefold(), kind_order[entry.kind], entry.name.casefold()))

    def filter_update_workbench(
        self,
        entries: list[UpdateWorkbenchEntry],
        *,
        query: str = "",
        kind: str = "all",
        activity: str = "all",
        diagnosis: str = "all",
        site_id: int | None = None,
        site_ids: set[int] | None = None,
        plugin_identifier: str = "",
    ) -> list[UpdateWorkbenchEntry]:
        normalized_query = query.strip().casefold()
        normalized_plugin_identifier = plugin_identifier.strip()
        selected_site_ids = site_ids if site_ids is not None else ({site_id} if site_id is not None else None)

        def matches(entry: UpdateWorkbenchEntry) -> bool:
            if selected_site_ids is not None and entry.site.id not in selected_site_ids:
                return False
            if normalized_plugin_identifier and entry.identifier != normalized_plugin_identifier:
                return False
            if kind != "all" and entry.kind != kind:
                return False
            if activity == "active" and entry.is_active is not True:
                return False
            if activity == "inactive" and entry.is_active is not False:
                return False
            if diagnosis == "attention" and not entry.official_mismatch:
                return False
            if diagnosis not in {"all", "attention"} and entry.diagnosis_status != diagnosis:
                return False
            if not normalized_query:
                return True

            haystack = " ".join((entry.site.domain, entry.name, entry.identifier)).casefold()
            return normalized_query in haystack

        return [entry for entry in entries if matches(entry)]

    def summarize_update_workbench(self, entries: list[UpdateWorkbenchEntry]) -> dict[str, int]:
        return {
            "total": len(entries),
            "available_updates": sum(1 for entry in entries if entry.update_available),
            "wordpress": sum(1 for entry in entries if entry.kind == "wordpress" and entry.update_available),
            "plugins": sum(1 for entry in entries if entry.kind == "plugin"),
            "plugin_updates": sum(1 for entry in entries if entry.kind == "plugin" and entry.update_available),
            "themes": sum(1 for entry in entries if entry.kind == "theme" and entry.update_available),
            "active_plugins": sum(1 for entry in entries if entry.kind == "plugin" and entry.is_active),
            "inactive_plugins": sum(1 for entry in entries if entry.kind == "plugin" and not entry.is_active),
            "official_versions_checked": sum(1 for entry in entries if entry.kind == "plugin" and entry.official_checked_at is not None),
            "official_version_mismatches": sum(1 for entry in entries if entry.kind == "plugin" and entry.official_mismatch),
        }

    @staticmethod
    def _attach_fleet_observed_versions(entries: list[UpdateWorkbenchEntry]) -> list[UpdateWorkbenchEntry]:
        """Expose newer versions seen on other sites without treating them as provider evidence."""
        observations: dict[str, dict[int, list[str]]] = {}
        for entry in entries:
            if entry.kind != "plugin" or not entry.identifier:
                continue
            versions = [version for version in (entry.current_version, entry.target_version) if version]
            if versions:
                observations.setdefault(entry.identifier, {}).setdefault(entry.site.id, []).extend(versions)

        enriched: list[UpdateWorkbenchEntry] = []
        for entry in entries:
            if entry.kind != "plugin" or not entry.identifier or not entry.current_version:
                enriched.append(entry)
                continue

            other_sites = {
                site_id: versions
                for site_id, versions in observations.get(entry.identifier, {}).items()
                if site_id != entry.site.id
            }
            observed_version = OfficialPluginVersionService._highest_version(
                version for versions in other_sites.values() for version in versions
            )
            if not observed_version or OfficialPluginVersionService._version_key(observed_version) <= OfficialPluginVersionService._version_key(entry.current_version):
                enriched.append(entry)
                continue

            observed_sites = sum(1 for versions in other_sites.values() if observed_version in versions)
            enriched.append(
                replace(
                    entry,
                    fleet_observed_version=observed_version,
                    fleet_observed_site_count=observed_sites,
                )
            )

        return enriched

    def _attach_official_plugin_versions(self, entries: list[UpdateWorkbenchEntry]) -> list[UpdateWorkbenchEntry]:
        plugin_files = [entry.identifier for entry in entries if entry.kind == "plugin" and entry.identifier]
        cached_versions = OfficialPluginVersionService(db=self.db).get_cached(plugin_files)
        enriched: list[UpdateWorkbenchEntry] = []

        for entry in entries:
            if entry.kind != "plugin":
                enriched.append(entry)
                continue

            reference = cached_versions.get(entry.identifier)
            if reference is None:
                enriched.append(entry)
                continue

            # Older releases stored a per-site update offer as an official version.
            # Keep that historical record out of comparison and execution decisions.
            if reference.source.startswith("Site update provider:"):
                official_version = ""
                official_source = "No trusted catalog version available"
            else:
                official_version = reference.official_version or ""
                official_source = reference.source

            mismatch, note = OfficialPluginVersionService.comparison(
                current_version=entry.current_version,
                reported_version=entry.target_version,
                official_version=official_version,
            )
            diagnosis_status, diagnosis_label, diagnosis_note = OfficialPluginVersionService.diagnosis(
                current_version=entry.current_version,
                reported_version=entry.target_version,
                official_version=official_version,
                official_source=official_source,
                execution_ready=entry.execution_ready,
                execution_note=entry.execution_note,
                is_jet_plugin=entry.identifier.startswith("jet-"),
            )
            enriched.append(
                replace(
                    entry,
                    official_version=official_version,
                    official_source=official_source,
                    official_checked_at=reference.checked_at,
                    official_mismatch=mismatch,
                    official_note=note,
                    diagnosis_status=diagnosis_status,
                    diagnosis_label=diagnosis_label,
                    diagnosis_note=diagnosis_note,
                )
            )

        return enriched

    @classmethod
    def _offer_kosmos_bridge_updates(cls, entries: list[UpdateWorkbenchEntry]) -> list[UpdateWorkbenchEntry]:
        """Use the Bridge's own published metadata as its fleet-wide update offer."""
        enriched: list[UpdateWorkbenchEntry] = []
        for entry in entries:
            if (
                entry.kind != "plugin"
                or entry.identifier != cls.KOSMOS_BRIDGE_PLUGIN_FILE
                or entry.official_source != OfficialPluginVersionService.KOSMOS_BRIDGE_SOURCE
                or not entry.official_version
                or not entry.current_version
            ):
                enriched.append(entry)
                continue

            catalog_version = entry.official_version
            current_key = OfficialPluginVersionService._version_key(entry.current_version)
            catalog_key = OfficialPluginVersionService._version_key(catalog_version)
            if current_key < catalog_key:
                execution_note = (
                    "Kosmos Bridge publishes this package directly. The website rechecks the "
                    "published package immediately before installation."
                )
                mismatch, official_note = OfficialPluginVersionService.comparison(
                    current_version=entry.current_version,
                    reported_version=catalog_version,
                    official_version=catalog_version,
                )
                diagnosis_status, diagnosis_label, diagnosis_note = OfficialPluginVersionService.diagnosis(
                    current_version=entry.current_version,
                    reported_version=catalog_version,
                    official_version=catalog_version,
                    official_source=entry.official_source,
                    execution_ready=True,
                    execution_note=execution_note,
                    is_jet_plugin=False,
                )
                enriched.append(
                    replace(
                        entry,
                        target_version=catalog_version,
                        update_available=True,
                        update_checked=True,
                        execution_ready=True,
                        execution_note=execution_note,
                        official_mismatch=mismatch,
                        official_note=official_note,
                        diagnosis_status=diagnosis_status,
                        diagnosis_label=diagnosis_label,
                        diagnosis_note=diagnosis_note,
                    )
                )
                continue

            if current_key == catalog_key and entry.update_available:
                mismatch, official_note = OfficialPluginVersionService.comparison(
                    current_version=entry.current_version,
                    reported_version="",
                    official_version=catalog_version,
                )
                diagnosis_status, diagnosis_label, diagnosis_note = OfficialPluginVersionService.diagnosis(
                    current_version=entry.current_version,
                    reported_version="",
                    official_version=catalog_version,
                    official_source=entry.official_source,
                    execution_ready=False,
                    execution_note="",
                    is_jet_plugin=False,
                )
                enriched.append(
                    replace(
                        entry,
                        target_version="",
                        update_available=False,
                        update_checked=True,
                        execution_ready=False,
                        execution_note="",
                        official_mismatch=mismatch,
                        official_note=official_note,
                        diagnosis_status=diagnosis_status,
                        diagnosis_label=diagnosis_label,
                        diagnosis_note=diagnosis_note,
                    )
                )
                continue

            enriched.append(entry)
        return enriched

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

    def refresh_verified_site_statuses(self, *, limit: int = 25) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Keep installed versions and update offers from the same refresh run."""
        return {
            "state": self.refresh_verified_site_states(limit=limit),
            "updates": self.refresh_verified_site_updates(limit=limit),
        }

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
        ability_names = frozenset(
            capability.ability_name
            for capability in site.capabilities
            if capability.provider == "kosmos-wordpress" and capability.ability_name
        )
        return FleetInventoryItem(
            site=site,
            snapshot=snapshot,
            update_snapshot=update_snapshot,
            plugins=plugins,
            ability_names=ability_names,
        )

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
