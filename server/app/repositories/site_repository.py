from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.site import Site
from app.models.site_backup_snapshot import SiteBackupSnapshot
from app.models.site_capability import SiteCapability
from app.models.site_connection import SiteConnection
from app.models.site_snapshot import SiteSnapshot
from app.models.site_update_snapshot import SiteUpdateSnapshot


class SiteRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_uuid(self, site_uuid: str) -> Site | None:
        statement = (
            select(Site)
            .options(
                selectinload(Site.connections),
                selectinload(Site.audit_entries),
                selectinload(Site.capabilities),
                selectinload(Site.snapshots),
                selectinload(Site.update_snapshots),
                selectinload(Site.backup_snapshots),
            )
            .where(Site.uuid == site_uuid)
        )
        return self.db.scalar(statement)

    def get_site(self, site_id: int) -> Site | None:
        statement = (
            select(Site)
            .options(
                selectinload(Site.connections),
                selectinload(Site.audit_entries),
                selectinload(Site.capabilities),
                selectinload(Site.snapshots),
                selectinload(Site.update_snapshots),
                selectinload(Site.backup_snapshots),
            )
            .where(Site.id == site_id)
        )
        return self.db.scalar(statement)

    def list_sites(self, limit: int = 1000) -> list[Site]:
        statement = (
            select(Site)
            .options(selectinload(Site.customer), selectinload(Site.connections), selectinload(Site.capabilities))
            .order_by(Site.updated_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement))

    def list_site_capabilities(self, site_id: int) -> list[SiteCapability]:
        statement = (
            select(SiteCapability)
            .where(SiteCapability.site_id == site_id)
            .order_by(SiteCapability.provider.asc(), SiteCapability.ability_name.asc())
        )
        return list(self.db.scalars(statement))

    def get_latest_site_snapshot(self, site_id: int) -> SiteSnapshot | None:
        statement = (
            select(SiteSnapshot)
            .where(SiteSnapshot.site_id == site_id)
            .order_by(SiteSnapshot.captured_at.desc())
            .limit(1)
        )
        return self.db.scalar(statement)

    def get_latest_snapshots_by_site_ids(self, site_ids: list[int]) -> dict[int, SiteSnapshot]:
        if not site_ids:
            return {}

        latest_captured_at = (
            select(
                SiteSnapshot.site_id,
                func.max(SiteSnapshot.captured_at).label("captured_at"),
            )
            .where(SiteSnapshot.site_id.in_(site_ids))
            .group_by(SiteSnapshot.site_id)
            .subquery()
        )
        statement = select(SiteSnapshot).join(
            latest_captured_at,
            (SiteSnapshot.site_id == latest_captured_at.c.site_id)
            & (SiteSnapshot.captured_at == latest_captured_at.c.captured_at),
        )
        snapshots = self.db.scalars(statement).all()
        return {snapshot.site_id: snapshot for snapshot in snapshots}

    def get_latest_site_update_snapshot(self, site_id: int) -> SiteUpdateSnapshot | None:
        statement = (
            select(SiteUpdateSnapshot)
            .where(SiteUpdateSnapshot.site_id == site_id)
            .order_by(SiteUpdateSnapshot.captured_at.desc())
            .limit(1)
        )
        return self.db.scalar(statement)

    def get_latest_site_backup_snapshot(self, site_id: int) -> SiteBackupSnapshot | None:
        statement = (
            select(SiteBackupSnapshot)
            .where(SiteBackupSnapshot.site_id == site_id)
            .order_by(SiteBackupSnapshot.captured_at.desc())
            .limit(1)
        )
        return self.db.scalar(statement)

    def get_latest_update_snapshots_by_site_ids(self, site_ids: list[int]) -> dict[int, SiteUpdateSnapshot]:
        if not site_ids:
            return {}

        latest_captured_at = (
            select(
                SiteUpdateSnapshot.site_id,
                func.max(SiteUpdateSnapshot.captured_at).label("captured_at"),
            )
            .where(SiteUpdateSnapshot.site_id.in_(site_ids))
            .group_by(SiteUpdateSnapshot.site_id)
            .subquery()
        )
        statement = select(SiteUpdateSnapshot).join(
            latest_captured_at,
            (SiteUpdateSnapshot.site_id == latest_captured_at.c.site_id)
            & (SiteUpdateSnapshot.captured_at == latest_captured_at.c.captured_at),
        )
        snapshots = self.db.scalars(statement).all()
        return {snapshot.site_id: snapshot for snapshot in snapshots}

    def get_latest_backup_snapshots_by_site_ids(self, site_ids: list[int]) -> dict[int, SiteBackupSnapshot]:
        if not site_ids:
            return {}

        latest_captured_at = (
            select(
                SiteBackupSnapshot.site_id,
                func.max(SiteBackupSnapshot.captured_at).label("captured_at"),
            )
            .where(SiteBackupSnapshot.site_id.in_(site_ids))
            .group_by(SiteBackupSnapshot.site_id)
            .subquery()
        )
        statement = select(SiteBackupSnapshot).join(
            latest_captured_at,
            (SiteBackupSnapshot.site_id == latest_captured_at.c.site_id)
            & (SiteBackupSnapshot.captured_at == latest_captured_at.c.captured_at),
        )
        snapshots = self.db.scalars(statement).all()
        return {snapshot.site_id: snapshot for snapshot in snapshots}

    def create_site_snapshot(
        self,
        *,
        site: Site,
        captured_at: datetime,
        wordpress_version: str | None,
        php_version: str | None,
        plugins_json: list,
        themes_json: list,
        environment_json: dict,
    ) -> SiteSnapshot:
        snapshot = SiteSnapshot(
            site=site,
            captured_at=captured_at,
            wordpress_version=wordpress_version,
            php_version=php_version,
            plugins_json=plugins_json,
            themes_json=themes_json,
            environment_json=environment_json,
        )
        self.db.add(snapshot)
        self.db.flush()
        return snapshot

    def create_site_update_snapshot(
        self,
        *,
        site: Site,
        captured_at: datetime,
        core_updates_json: list,
        plugin_updates_json: list,
        theme_updates_json: list,
        summary_json: dict,
    ) -> SiteUpdateSnapshot:
        snapshot = SiteUpdateSnapshot(
            site=site,
            captured_at=captured_at,
            core_updates_json=core_updates_json,
            plugin_updates_json=plugin_updates_json,
            theme_updates_json=theme_updates_json,
            summary_json=summary_json,
        )
        self.db.add(snapshot)
        self.db.flush()
        return snapshot

    def create_site_backup_snapshot(
        self,
        *,
        site: Site,
        captured_at: datetime,
        provider: str,
        provider_installed: bool,
        provider_active: bool,
        backup_available: bool,
        backup_complete: bool,
        backup_at: datetime | None,
        backup_count: int,
        components_json: list,
        summary_json: dict,
    ) -> SiteBackupSnapshot:
        snapshot = SiteBackupSnapshot(
            site=site,
            captured_at=captured_at,
            provider=provider,
            provider_installed=provider_installed,
            provider_active=provider_active,
            backup_available=backup_available,
            backup_complete=backup_complete,
            backup_at=backup_at,
            backup_count=backup_count,
            components_json=components_json,
            summary_json=summary_json,
        )
        self.db.add(snapshot)
        self.db.flush()
        return snapshot

    def sync_site_capabilities(
        self,
        *,
        site: Site,
        provider: str,
        abilities: list[dict],
        discovered_at: datetime,
    ) -> list[SiteCapability]:
        existing = {capability.ability_name: capability for capability in site.capabilities if capability.provider == provider}
        seen: set[str] = set()

        for ability in abilities:
            normalized_ability = self._normalize_ability_schema(ability)
            ability_name = str(normalized_ability.get("name", "")).strip()
            if not ability_name:
                continue

            annotations = normalized_ability.get("meta", {}).get("annotations", {})
            capability = existing.get(ability_name)
            if capability is None:
                capability = SiteCapability(
                    site=site,
                    capability=ability_name,
                    provider=provider,
                    ability_name=ability_name,
                    ability_schema=normalized_ability,
                    read_only=False,
                    destructive=False,
                    last_discovered_at=discovered_at,
                )
                self.db.add(capability)

            capability.capability = ability_name
            capability.ability_schema = normalized_ability
            capability.read_only = bool(annotations.get("readonly", False))
            capability.destructive = bool(annotations.get("destructive", False))
            capability.last_discovered_at = discovered_at
            seen.add(ability_name)

        for ability_name, capability in existing.items():
            if ability_name not in seen:
                self.db.delete(capability)

        self.db.flush()
        return self.list_site_capabilities(site.id)

    def _normalize_ability_schema(self, ability: dict) -> dict:
        normalized = dict(ability)

        for field_name in ("input_schema", "output_schema", "meta"):
            value = normalized.get(field_name)
            if isinstance(value, dict):
                continue
            if isinstance(value, list) and not value:
                normalized[field_name] = {}
                continue
            if value is None:
                normalized[field_name] = {}

        return normalized

    def get_or_create_connection(self, site: Site, provider: str) -> SiteConnection:
        for connection in site.connections:
            if connection.provider == provider:
                return connection

        connection = SiteConnection(site=site, provider=provider, endpoint="", auth_type="hmac-sha256", encrypted_credentials="")
        self.db.add(connection)
        return connection

    def get_dashboard_summary(self) -> dict[str, int]:
        total = self.db.scalar(select(func.count()).select_from(Site)) or 0
        pending = self.db.scalar(select(func.count()).select_from(Site).where(Site.status == "pending")) or 0
        verified = self.db.scalar(select(func.count()).select_from(Site).where(Site.status == "verified")) or 0
        stale_cutoff = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)
        unknown = self.db.scalar(
            select(func.count()).select_from(Site).where(
                (Site.last_seen_at.is_(None)) | (Site.last_seen_at < stale_cutoff)
            )
        ) or 0
        return {
            "total_sites": total,
            "pending_sites": pending,
            "verified_sites": verified,
            "unknown_sites": unknown,
        }
