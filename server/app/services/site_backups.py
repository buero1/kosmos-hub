from datetime import UTC, datetime
from typing import Any

import re

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


class SiteBackupService:
    """Store provider-reported backup metadata without handling backup data."""

    ABILITY_NAME = "kosmos-bridge/get-updraftplus-backup-status"
    LIST_ABILITY_NAME = "kosmos-bridge/list-updraftplus-backups"
    PROVIDER = "updraftplus"
    COMPONENTS = {"database", "plugins", "themes", "uploads", "others"}
    BACKUP_NONCE_PATTERN = re.compile(r"[a-f0-9]{12}")

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

        try:
            payload = self.proxy.execute_ability(site_id, self.LIST_ABILITY_NAME, {}, timeout_seconds=30)
            result = self._status_result_from_backup_list(payload.get("result", {}))
        except SiteMcpProxyError as exc:
            if exc.code != "KOSMOS_BRIDGE_ABILITY_NOT_FOUND":
                raise
            payload = self.proxy.execute_ability(site_id, self.ABILITY_NAME, {}, timeout_seconds=30)
            result = payload.get("result", {})
            result = result if isinstance(result, dict) else {}
            result["backup_list_available"] = False
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
        retention_protected = self._as_bool(result.get("retention_protected"))
        message = self._string_or_none(result.get("message"))
        backup_sets = self._backup_sets_from(result.get("backups"))
        if not backup_sets and backup_at is not None:
            backup_sets = [
                {
                    "backup_at": backup_at.isoformat(),
                    "complete": complete,
                    "retention_protected": retention_protected,
                    "components": components,
                }
            ]
        if backup_sets:
            backup_count = max(backup_count, len(backup_sets))

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
                "retention_protected": retention_protected,
                "message": message,
                "backup_list_available": self._as_bool(result.get("backup_list_available", False)),
                "backups": backup_sets,
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
                f"available={available}, complete={complete}, retention_protected={retention_protected}."
            ),
        )
        self.db.commit()
        return {"site_id": site.id, "refreshed_at": captured_at, "snapshot": snapshot}

    @classmethod
    def _status_result_from_backup_list(cls, result: object) -> dict[str, Any]:
        result = result if isinstance(result, dict) else {}
        backup_sets = cls._backup_sets_from(result.get("backups"))
        latest_complete = next((backup for backup in backup_sets if backup["complete"]), None)
        return {
            "installed": result.get("installed"),
            "active": result.get("active"),
            "available": latest_complete is not None,
            "complete": latest_complete is not None,
            "retention_protected": latest_complete["retention_protected"] if latest_complete else False,
            "latest_backup_at": latest_complete["backup_at"] if latest_complete else "",
            "backup_count": len(backup_sets),
            "components": latest_complete["components"] if latest_complete else [],
            "message": result.get("message"),
            "backups": backup_sets,
            "backup_list_available": True,
        }

    @classmethod
    def _backup_sets_from(cls, value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []

        backup_sets: list[dict[str, Any]] = []
        for raw_backup in value:
            if not isinstance(raw_backup, dict):
                continue
            backup_at = cls._parse_datetime(raw_backup.get("backup_at"))
            if backup_at is None:
                continue
            backup_set = {
                "backup_at": backup_at.isoformat(),
                "complete": cls._as_bool(raw_backup.get("complete")),
                "retention_protected": cls._as_bool(raw_backup.get("retention_protected")),
                "components": cls._components_from(raw_backup.get("components")),
            }
            backup_nonce = raw_backup.get("backup_nonce")
            backup_timestamp = raw_backup.get("backup_timestamp")
            if (
                isinstance(backup_nonce, str)
                and cls.BACKUP_NONCE_PATTERN.fullmatch(backup_nonce)
                and isinstance(backup_timestamp, int)
                and not isinstance(backup_timestamp, bool)
                and backup_timestamp > 0
            ):
                backup_set["backup_nonce"] = backup_nonce
                backup_set["backup_timestamp"] = backup_timestamp
            backup_sets.append(backup_set)
        return sorted(backup_sets, key=lambda backup: backup["backup_at"], reverse=True)

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
