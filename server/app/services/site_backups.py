from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


class SiteBackupService:
    """Store provider-reported backup metadata without handling backup data."""

    ABILITY_NAME = "kosmos-bridge/get-updraftplus-backup-status"
    PROVIDER = "updraftplus"
    COMPONENTS = {"database", "plugins", "themes", "uploads", "others"}

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.repository = SiteRepository(db)
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    def get_latest_site_backup_snapshot(self, site_id: int):
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)
        return self.repository.get_latest_site_backup_snapshot(site_id)

    def refresh_site_backup_status(self, site_id: int):
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)

        payload = self.proxy.execute_ability(site_id, self.ABILITY_NAME, None, timeout_seconds=30)
        result = payload.get("result", {})
        result = result if isinstance(result, dict) else {}
        return self.store_backup_status_result(site_id, result)

    def store_backup_status_result(self, site_id: int, result: dict[str, Any]):
        """Persist a provider result that was already fetched by a scoped workflow."""
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)

        captured_at = datetime.now(UTC)

        installed = self._as_bool(result.get("installed"))
        active = self._as_bool(result.get("active"))
        complete = self._as_bool(result.get("complete"))
        backup_at = self._parse_datetime(result.get("latest_backup_at"))
        available = self._as_bool(result.get("available")) and complete and backup_at is not None
        components = self._components_from(result.get("components"))
        backup_count = self._non_negative_int(result.get("backup_count"))
        message = self._string_or_none(result.get("message"))

        snapshot = self.repository.create_site_backup_snapshot(
            site=site,
            captured_at=captured_at,
            provider=self.PROVIDER,
            provider_installed=installed,
            provider_active=active,
            backup_available=available,
            backup_complete=complete,
            backup_at=backup_at,
            backup_count=backup_count,
            components_json=components,
            summary_json={
                "reported_at": self._string_or_none(result.get("reported_at")),
                "message": message,
            },
        )
        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-hub",
            source="hub",
            action="refresh-site-backup-status",
            result="ok",
            detail=(
                f"Stored UpdraftPlus backup status for {site.domain}: "
                f"available={available}, complete={complete}."
            ),
        )
        self.db.commit()
        return {"site_id": site.id, "refreshed_at": captured_at, "snapshot": snapshot}

    @staticmethod
    def _as_bool(value: object) -> bool:
        return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"})

    @staticmethod
    def _non_negative_int(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _string_or_none(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _components_from(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item in cls.COMPONENTS]

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)
