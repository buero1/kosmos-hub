from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


class SiteUpdateService:
    ABILITY_NAME = "kosmos-bridge/get-available-updates"

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.repository = SiteRepository(db)
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    def get_latest_site_update_snapshot(self, site_id: int):
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)
        return self.repository.get_latest_site_update_snapshot(site_id)

    def refresh_site_updates(self, site_id: int):
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)

        payload = self.proxy.execute_ability(site_id, self.ABILITY_NAME, None, timeout_seconds=60)
        result = payload.get("result", {})
        result = result if isinstance(result, dict) else {}

        core_updates = self._updates_from(result.get("wordpress"))
        plugin_updates = self._updates_from(result.get("plugins"))
        theme_updates = self._updates_from(result.get("themes"))
        summary = {
            "wordpress": len(core_updates),
            "plugins": len(plugin_updates),
            "themes": len(theme_updates),
            "total": len(core_updates) + len(plugin_updates) + len(theme_updates),
            "reported_at": self._string_or_none(result.get("reported_at")),
        }

        captured_at = datetime.now(UTC)
        snapshot = self.repository.create_site_update_snapshot(
            site=site,
            captured_at=captured_at,
            core_updates_json=core_updates,
            plugin_updates_json=plugin_updates,
            theme_updates_json=theme_updates,
            summary_json=summary,
        )

        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-hub",
            source="hub",
            action="refresh-site-updates",
            result="ok",
            detail=(
                f"Stored update snapshot for {site.domain}: {summary['wordpress']} WordPress, "
                f"{summary['plugins']} plugin, {summary['themes']} theme updates."
            ),
        )
        self.db.commit()
        return {
            "site_id": site.id,
            "refreshed_at": captured_at,
            "snapshot": snapshot,
        }

    def record_confirmed_direct_update(
        self,
        *,
        site_id: int,
        update_kind: str,
        identifier: str,
    ) -> None:
        """Remove one completed update from the stored offer without fetching new offers."""
        site = self.repository.get_site(site_id)
        snapshot = self.repository.get_latest_site_update_snapshot(site_id)
        if site is None or snapshot is None:
            return

        core_updates = [dict(update) for update in snapshot.core_updates_json if isinstance(update, dict)]
        plugin_updates = [dict(update) for update in snapshot.plugin_updates_json if isinstance(update, dict)]
        theme_updates = [dict(update) for update in snapshot.theme_updates_json if isinstance(update, dict)]
        if update_kind == "wordpress":
            updated_core = []
            changed = bool(core_updates)
        elif update_kind == "plugin":
            updated_core = core_updates
            plugin_updates, changed = self._without_confirmed_update(
                plugin_updates,
                identifier_field="plugin_file",
                identifier=identifier,
            )
        elif update_kind == "theme":
            updated_core = core_updates
            theme_updates, changed = self._without_confirmed_update(
                theme_updates,
                identifier_field="stylesheet",
                identifier=identifier,
            )
        else:
            return

        if not changed:
            return

        summary = dict(snapshot.summary_json) if isinstance(snapshot.summary_json, dict) else {}
        summary.update(
            {
                "wordpress": len(updated_core),
                "plugins": len(plugin_updates),
                "themes": len(theme_updates),
                "total": len(updated_core) + len(plugin_updates) + len(theme_updates),
                "locally_reconciled": True,
            }
        )
        self.repository.create_site_update_snapshot(
            site=site,
            captured_at=datetime.now(UTC),
            core_updates_json=updated_core,
            plugin_updates_json=plugin_updates,
            theme_updates_json=theme_updates,
            summary_json=summary,
        )
        self.db.flush()

    @staticmethod
    def _without_confirmed_update(
        updates: list[dict[str, Any]],
        *,
        identifier_field: str,
        identifier: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        remaining = [
            update
            for update in updates
            if str(update.get(identifier_field, "")).strip() != identifier
        ]
        return remaining, len(remaining) != len(updates)

    def _updates_from(self, value: object) -> list[dict[str, Any]]:
        if not isinstance(value, dict):
            return []
        updates = value.get("updates", [])
        if not isinstance(updates, list):
            return []
        return [dict(item) for item in updates if isinstance(item, dict)]

    def _string_or_none(self, value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
