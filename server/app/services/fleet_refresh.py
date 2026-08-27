import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.models.fleet_refresh_run import FleetRefreshRun, FleetRefreshRunStatus
from app.models.hub_user import HubUser
from app.models.site import Site, SiteStatus
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.crocoblock_license import CrocoblockLicenseService
from app.services.fleet_inventory import FleetInventoryService
from app.services.fleet_refresh_settings import FleetRefreshRuntimeSettings, FleetRefreshSettingsService
from app.services.official_plugin_versions import OfficialPluginVersionService
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService


class FleetRefreshService:
    """Runs bounded, persisted fleet refreshes without blocking the web request."""

    MODE_NORMAL = "normal"
    MODE_FULL = "full"
    SCHEDULED_REQUESTED_BY = "kosmos-scheduler"
    BERLIN_TIMEZONE = ZoneInfo("Europe/Berlin")
    def __init__(self, *, db: Session):
        self.db = db

    def create_run(self, *, actor: HubUser, mode: str) -> tuple[FleetRefreshRun, bool]:
        if actor.role != "admin":
            raise ValueError("Only Hub administrators can start a fleet refresh.")
        self._validate_mode(mode)

        active_run = self.db.scalar(
            select(FleetRefreshRun)
            .where(FleetRefreshRun.status.in_((FleetRefreshRunStatus.queued.value, FleetRefreshRunStatus.running.value)))
            .order_by(FleetRefreshRun.created_at.asc())
            .limit(1)
        )
        if active_run is not None:
            return active_run, False

        run = FleetRefreshRun(
            mode=mode,
            status=FleetRefreshRunStatus.queued.value,
            requested_by=actor.username,
            allow_provider_activation=True,
            result_json=self._initial_result(mode),
        )
        self.db.add(run)
        self.db.flush()
        return run, True

    def get_run(self, run_id: int) -> FleetRefreshRun | None:
        return self.db.get(FleetRefreshRun, run_id)

    def get_latest_run(self) -> FleetRefreshRun | None:
        return self.db.scalar(select(FleetRefreshRun).order_by(FleetRefreshRun.created_at.desc()).limit(1))

    @classmethod
    def queue_scheduled_run(cls) -> int | None:
        with SessionLocal() as db:
            runtime_settings = FleetRefreshSettingsService(db=db).get_runtime_settings()
            if not runtime_settings.auto_refresh_enabled:
                return None

            active_run = db.scalar(
                select(FleetRefreshRun)
                .where(FleetRefreshRun.status.in_((FleetRefreshRunStatus.queued.value, FleetRefreshRunStatus.running.value)))
                .order_by(FleetRefreshRun.created_at.asc())
                .limit(1)
            )
            if active_run is not None:
                return None

            now = datetime.now(UTC)
            if not cls._is_scheduled_run_due(db=db, runtime_settings=runtime_settings, now=now):
                return None

            run = FleetRefreshRun(
                mode=cls.MODE_NORMAL,
                status=FleetRefreshRunStatus.queued.value,
                requested_by=cls.SCHEDULED_REQUESTED_BY,
                allow_provider_activation=False,
                result_json=cls._initial_result(cls.MODE_NORMAL),
            )
            db.add(run)
            db.commit()
            return run.id

    @classmethod
    def _is_scheduled_run_due(
        cls,
        *,
        db: Session,
        runtime_settings: FleetRefreshRuntimeSettings,
        now: datetime,
    ) -> bool:
        berlin_now = now.astimezone(cls.BERLIN_TIMEZONE)
        scheduled_time = datetime.strptime(runtime_settings.auto_refresh_time, "%H:%M").time()
        if berlin_now.time().replace(tzinfo=None) < scheduled_time:
            return False

        latest_scheduled = db.scalar(
            select(FleetRefreshRun)
            .where(FleetRefreshRun.requested_by == cls.SCHEDULED_REQUESTED_BY)
            .order_by(FleetRefreshRun.created_at.desc())
            .limit(1)
        )
        if latest_scheduled is not None:
            last_scheduled_day = cls._as_utc(latest_scheduled.created_at).astimezone(cls.BERLIN_TIMEZONE).date()
            interval_days = runtime_settings.auto_refresh_interval_hours // 24
            return (berlin_now.date() - last_scheduled_day).days >= interval_days

        # Do not add an unnecessary automatic run immediately after a manual full refresh.
        latest_any_run = db.scalar(select(FleetRefreshRun).order_by(FleetRefreshRun.created_at.desc()).limit(1))
        if latest_any_run is not None and now - cls._as_utc(latest_any_run.created_at) < timedelta(hours=24):
            return False
        return True

    @classmethod
    def process_next_queued_run(cls) -> int | None:
        with SessionLocal() as db:
            run = db.scalar(
                select(FleetRefreshRun)
                .where(FleetRefreshRun.status == FleetRefreshRunStatus.queued.value)
                .order_by(FleetRefreshRun.created_at.asc())
                .limit(1)
            )
            run_id = run.id if run is not None else None
        if run_id is None:
            return None
        cls.process_run(run_id)
        return run_id

    @classmethod
    def recover_interrupted_runs(cls) -> int:
        with SessionLocal() as db:
            result = db.execute(
                update(FleetRefreshRun)
                .where(FleetRefreshRun.status == FleetRefreshRunStatus.running.value)
                .values(status=FleetRefreshRunStatus.queued.value, started_at=None)
            )
            db.commit()
            return int(result.rowcount or 0)

    @classmethod
    def process_run(cls, run_id: int) -> None:
        run_data = cls._claim_run(run_id)
        if run_data is None:
            return

        try:
            result = cls._perform_run(**run_data)
        except Exception as exc:
            cls._finish_run(run_id, status=FleetRefreshRunStatus.failed.value, result=None, error_message=str(exc))
            return

        cls._finish_run(run_id, status=FleetRefreshRunStatus.succeeded.value, result=result, error_message=None)

    @classmethod
    def _claim_run(cls, run_id: int) -> dict[str, Any] | None:
        with SessionLocal() as db:
            claim = db.execute(
                update(FleetRefreshRun)
                .where(FleetRefreshRun.id == run_id, FleetRefreshRun.status == FleetRefreshRunStatus.queued.value)
                .values(status=FleetRefreshRunStatus.running.value, started_at=datetime.now(UTC), error_message=None)
            )
            if not claim.rowcount:
                db.rollback()
                return None
            run = db.get(FleetRefreshRun, run_id)
            db.commit()
            if run is None:
                return None
            return {
                "run_id": run.id,
                "mode": run.mode,
                "requested_by": run.requested_by,
                "allow_provider_activation": run.allow_provider_activation,
            }

    @classmethod
    def _perform_run(
        cls,
        *,
        run_id: int,
        mode: str,
        requested_by: str,
        allow_provider_activation: bool,
    ) -> dict[str, Any]:
        cls._validate_mode(mode)
        force = mode == cls.MODE_FULL
        with SessionLocal() as db:
            runtime_settings = FleetRefreshSettingsService(db=db).get_runtime_settings()
        result = cls._initial_result(mode, runtime_settings=runtime_settings)
        targets, cached_count, skipped_count = cls._site_targets(force=force, runtime_settings=runtime_settings)
        result["sites"].update(
            {
                "total": len(targets) + cached_count,
                "completed": cached_count,
                "cached": cached_count,
                "skipped": skipped_count,
            }
        )
        cls._store_progress(run_id, result)

        if targets:
            max_workers = min(runtime_settings.max_parallel_site_checks, len(targets))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(cls._refresh_one_site, target): target for target in targets}
                for future in as_completed(futures):
                    target = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = {"site_id": target["site_id"], "domain": target["domain"], "state": "failed", "updates": "skipped", "error": str(exc)}
                    cls._record_site_outcome(result, outcome)
                    cls._store_progress(run_id, result)

        cls._refresh_provider_evidence(
            run_id=run_id,
            result=result,
            force=force,
            requested_by=requested_by,
            allow_provider_activation=allow_provider_activation,
            runtime_settings=runtime_settings,
        )
        return result

    @classmethod
    def _site_targets(
        cls,
        *,
        force: bool,
        runtime_settings: FleetRefreshRuntimeSettings,
    ) -> tuple[list[dict[str, Any]], int, int]:
        with SessionLocal() as db:
            repository = SiteRepository(db)
            sites = repository.list_sites(limit=1000)
            snapshots = repository.get_latest_snapshots_by_site_ids([site.id for site in sites])
            update_snapshots = repository.get_latest_update_snapshots_by_site_ids([site.id for site in sites])
            now = datetime.now(UTC)
            targets: list[dict[str, Any]] = []
            cached_count = 0
            skipped_count = 0
            for site in sites:
                if site.status != SiteStatus.verified.value:
                    skipped_count += 1
                    continue
                max_age = timedelta(minutes=runtime_settings.site_status_max_age_minutes)
                state_is_fresh = cls._is_fresh(snapshots.get(site.id), now=now, max_age=max_age)
                updates_are_fresh = cls._is_fresh(update_snapshots.get(site.id), now=now, max_age=max_age)
                if not force and state_is_fresh and updates_are_fresh:
                    cached_count += 1
                    continue
                targets.append({"site_id": site.id, "domain": site.domain})
            return targets, cached_count, skipped_count

    @classmethod
    def _refresh_one_site(cls, target: dict[str, Any]) -> dict[str, Any]:
        site_id = int(target["site_id"])
        with SessionLocal() as db:
            repository = SiteRepository(db)
            site = repository.get_site(site_id)
            if site is None:
                return {"site_id": site_id, "domain": target["domain"], "state": "failed", "updates": "skipped", "error": "Site no longer exists."}

            try:
                SiteInventoryService(db=db, cipher=get_secret_cipher()).refresh_site_state(site_id)
            except SiteMcpProxyError as exc:
                return {"site_id": site_id, "domain": site.domain, "state": "failed", "updates": "skipped", "error": exc.message}

            if not cls._bridge_supports_updates(site.bridge_version):
                return {"site_id": site_id, "domain": site.domain, "state": "refreshed", "updates": "skipped", "error": "Bridge update support is unavailable."}

            try:
                SiteUpdateService(db=db, cipher=get_secret_cipher()).refresh_site_updates(site_id)
            except SiteMcpProxyError as exc:
                return {"site_id": site_id, "domain": site.domain, "state": "refreshed", "updates": "failed", "error": exc.message}
            return {"site_id": site_id, "domain": site.domain, "state": "refreshed", "updates": "refreshed"}

    @classmethod
    def _record_site_outcome(cls, result: dict[str, Any], outcome: dict[str, Any]) -> None:
        result["sites"]["completed"] += 1
        result["last_site"] = outcome.get("domain", "")
        if outcome.get("state") == "refreshed":
            result["state"]["refreshed"] += 1
            result["sites"]["refreshed"] += 1
        else:
            result["state"]["failed"] += 1
            result["sites"]["failed"] += 1
        if outcome.get("updates") == "refreshed":
            result["updates"]["refreshed"] += 1
        elif outcome.get("updates") == "failed":
            result["updates"]["failed"] += 1
        else:
            result["updates"]["skipped"] += 1
        if outcome.get("error"):
            result["errors"].append({"site": outcome.get("domain", "unknown"), "detail": str(outcome["error"])[:240]})

    @classmethod
    def _refresh_provider_evidence(
        cls,
        *,
        run_id: int,
        result: dict[str, Any],
        force: bool,
        requested_by: str,
        allow_provider_activation: bool,
        runtime_settings: FleetRefreshRuntimeSettings,
    ) -> None:
        with SessionLocal() as db:
            inventory = FleetInventoryService(db=db, cipher=get_secret_cipher())
            items = inventory.list_items(limit=1000)
            entries = inventory.build_update_workbench(items)
            version_service = OfficialPluginVersionService(db=db)
            jet_site_ids = cls._jet_sites_requiring_provider(
                items=items,
                entries=entries,
                version_service=version_service,
                force=force,
                provider_max_age=timedelta(hours=runtime_settings.official_version_max_age_hours),
            )
            result["crocoblock"]["eligible"] = len(jet_site_ids)
            if jet_site_ids and allow_provider_activation:
                crocoblock = CrocoblockLicenseService(db=db, cipher=get_secret_cipher()).refresh_version_evidence(
                    actor=requested_by,
                    site_ids=jet_site_ids,
                )
                result["crocoblock"].update(
                    {
                        "refreshed": crocoblock["refreshed"],
                        "failed": crocoblock["failed"],
                        "catalog_versions": len(crocoblock["versions"]),
                    }
                )
                items = inventory.list_items(limit=1000)
            elif jet_site_ids:
                result["crocoblock"]["cached"] = len(jet_site_ids)

            official = version_service.refresh_for_inventory(
                items,
                force=force,
                max_age=timedelta(hours=runtime_settings.official_version_max_age_hours),
            )
            if jet_site_ids and allow_provider_activation:
                result["crocoblock"]["catalog_versions"] = version_service.record_provider_versions(
                    crocoblock["versions"],
                    source="Crocoblock Jet Dashboard",
                )
            result["official_versions"] = official
            refreshed_entries = inventory.build_update_workbench(inventory.list_items(limit=1000))
            result["mismatches"] = sum(1 for entry in refreshed_entries if entry.official_mismatch)
            write_audit_log(
                db,
                site=None,
                actor=requested_by,
                source="hub-worker",
                action="fleet-refresh",
                result="success",
                detail=(
                    f"Mode={result['mode']}; refreshed {result['state']['refreshed']} site states and "
                    f"{result['updates']['refreshed']} update offers; diagnosed {result['mismatches']} mismatches."
                ),
            )
            db.commit()
        cls._store_progress(run_id, result)

    @classmethod
    def _jet_sites_requiring_provider(
        cls,
        *,
        items: list[Any],
        entries: list[Any],
        version_service: OfficialPluginVersionService,
        force: bool,
        provider_max_age: timedelta,
    ) -> set[int]:
        jet_entries = [entry for entry in entries if entry.kind == "plugin" and entry.identifier.startswith("jet-")]
        cached_versions = version_service.get_cached([entry.identifier for entry in jet_entries])
        now = datetime.now(UTC)
        required_site_ids: set[int] = set()
        for entry in jet_entries:
            reference = cached_versions.get(entry.identifier)
            if force or entry.official_mismatch or not OfficialPluginVersionService._is_fresh(
                reference,
                now=now,
                max_age=provider_max_age,
            ):
                required_site_ids.add(entry.site.id)
        return required_site_ids

    @classmethod
    def _store_progress(cls, run_id: int, result: dict[str, Any]) -> None:
        with SessionLocal() as db:
            run = db.get(FleetRefreshRun, run_id)
            if run is not None and run.status == FleetRefreshRunStatus.running.value:
                run.result_json = copy.deepcopy(result)
                db.commit()

    @classmethod
    def _finish_run(
        cls,
        run_id: int,
        *,
        status: str,
        result: dict[str, Any] | None,
        error_message: str | None,
    ) -> None:
        with SessionLocal() as db:
            run = db.get(FleetRefreshRun, run_id)
            if run is None:
                return
            run.status = status
            run.completed_at = datetime.now(UTC)
            run.error_message = error_message
            if result is not None:
                run.result_json = copy.deepcopy(result)
            db.commit()

    @classmethod
    def _initial_result(
        cls,
        mode: str,
        *,
        runtime_settings: FleetRefreshRuntimeSettings | None = None,
    ) -> dict[str, Any]:
        runtime_settings = runtime_settings or FleetRefreshRuntimeSettings()
        return {
            "mode": mode,
            "settings": {
                "site_status_max_age_minutes": runtime_settings.site_status_max_age_minutes,
                "official_version_max_age_hours": runtime_settings.official_version_max_age_hours,
                "max_parallel_site_checks": runtime_settings.max_parallel_site_checks,
            },
            "sites": {"total": 0, "completed": 0, "refreshed": 0, "cached": 0, "failed": 0, "skipped": 0},
            "state": {"refreshed": 0, "failed": 0},
            "updates": {"refreshed": 0, "failed": 0, "skipped": 0},
            "crocoblock": {"eligible": 0, "refreshed": 0, "cached": 0, "failed": 0, "catalog_versions": 0},
            "official_versions": {"total": 0, "checked": 0, "cached": 0, "wordpress_org": 0, "provider_offer": 0, "unavailable": 0, "failed": 0},
            "mismatches": 0,
            "last_site": "",
            "errors": [],
        }

    @staticmethod
    def _is_fresh(snapshot: Any, *, now: datetime, max_age: timedelta) -> bool:
        captured_at = getattr(snapshot, "captured_at", None)
        if captured_at is None:
            return False
        timestamp = captured_at if captured_at.tzinfo is not None else captured_at.replace(tzinfo=UTC)
        return now - timestamp <= max_age

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _bridge_supports_updates(version: str | None) -> bool:
        if not version:
            return False
        try:
            current = tuple(int(part) for part in version.split("-", 1)[0].split("."))
        except ValueError:
            return False
        return current >= (0, 3, 7)

    @classmethod
    def _validate_mode(cls, mode: str) -> None:
        if mode not in (cls.MODE_NORMAL, cls.MODE_FULL):
            raise ValueError("Refresh mode must be normal or full.")
