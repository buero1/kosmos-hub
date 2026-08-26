from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


class SiteInventoryService:
    INSTALLED_PLUGINS_ABILITY = "kosmos-bridge/list-installed-plugins"
    LEGACY_ACTIVE_PLUGINS_ABILITY = "kosmos-bridge/list-active-plugins"

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.repository = SiteRepository(db)
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    def list_site_capabilities(self, site_id: int):
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)
        return self.repository.list_site_capabilities(site_id)

    def get_latest_site_snapshot(self, site_id: int):
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)
        return self.repository.get_latest_site_snapshot(site_id)

    def refresh_site_inventory(self, site_id: int):
        payload = self.proxy.discover_abilities(site_id)
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)

        discovered_at = datetime.now(UTC)
        capabilities = self.repository.sync_site_capabilities(
            site=site,
            provider="kosmos-wordpress",
            abilities=payload.get("abilities", []),
            discovered_at=discovered_at,
        )

        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-hub",
            source="hub",
            action="refresh-site-inventory",
            result="ok",
            detail=f"Stored {len(capabilities)} discovered abilities for {site.domain}.",
        )
        self.db.commit()
        return {
            "site_id": site.id,
            "provider": "kosmos-wordpress",
            "refreshed_at": discovered_at,
            "items": capabilities,
        }

    def refresh_site_state(self, site_id: int):
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)

        previous_bridge_version = site.bridge_version
        environment_payload = self.proxy.execute_ability(site_id, "kosmos-bridge/get-environment-info", None)
        try:
            plugins_payload = self.proxy.execute_ability(site_id, self.INSTALLED_PLUGINS_ABILITY, None)
            plugins_result = plugins_payload.get("result", {})
            plugins = self._normalize_plugins(plugins_result, default_active=False)
        except SiteMcpProxyError as exc:
            if exc.code != "KOSMOS_BRIDGE_ABILITY_NOT_FOUND":
                raise
            plugins_payload = self.proxy.execute_ability(site_id, self.LEGACY_ACTIVE_PLUGINS_ABILITY, None)
            plugins_result = plugins_payload.get("result", {})
            plugins = self._normalize_plugins(plugins_result, default_active=True)

        environment = environment_payload.get("result", {})

        refreshed_at = datetime.now(UTC)
        site.wordpress_version = self._string_or_none(environment.get("wordpress_version"))
        site.php_version = self._string_or_none(environment.get("php_version"))
        site.bridge_version = self._string_or_none(environment.get("bridge_version"))
        site.last_seen_at = refreshed_at

        snapshot = self.repository.create_site_snapshot(
            site=site,
            captured_at=refreshed_at,
            wordpress_version=site.wordpress_version,
            php_version=site.php_version,
            plugins_json=plugins,
            themes_json=[],
            environment_json=environment if isinstance(environment, dict) else {},
        )

        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-hub",
            source="hub",
            action="refresh-site-state",
            result="ok",
            detail=f"Stored site snapshot for {site.domain} with {len(snapshot.plugins_json)} installed plugins.",
        )
        self.db.commit()

        capability_refresh_error = ""
        if site.bridge_version != previous_bridge_version:
            try:
                self.refresh_site_inventory(site_id)
            except SiteMcpProxyError as exc:
                capability_refresh_error = exc.message

        return {
            "site_id": site.id,
            "refreshed_at": refreshed_at,
            "snapshot": snapshot,
            "capabilities_refreshed": not capability_refresh_error and site.bridge_version != previous_bridge_version,
            "capability_refresh_error": capability_refresh_error,
        }

    def _string_or_none(self, value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _normalize_plugins(result: object, *, default_active: bool) -> list[dict]:
        if not isinstance(result, dict):
            return []
        raw_plugins = result.get("plugins", [])
        if not isinstance(raw_plugins, list):
            return []

        plugins: list[dict] = []
        for raw_plugin in raw_plugins:
            if not isinstance(raw_plugin, dict):
                continue
            plugin = dict(raw_plugin)
            plugin["active"] = plugin["active"] if isinstance(plugin.get("active"), bool) else default_active
            plugins.append(plugin)
        return plugins
