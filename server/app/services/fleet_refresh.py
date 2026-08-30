import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.models.fleet_refresh_run import FleetRefreshRun, FleetRefreshRunStatus, FleetRefreshSiteResult
from app.models.hub_user import HubUser
from app.models.site import Site, SiteStatus
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.crocoblock_license import CrocoblockLicenseService
from app.services.fleet_inventory import FleetInventoryService
from app.services.fleet_refresh_settings import FleetRefreshRuntimeSettings, FleetRefreshSettingsService
from app.services.official_plugin_versions import OfficialPluginVersionService
from app.services.site_backups import SiteBackupService
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService
from app.services.site_users import SiteUserService


class FleetRefreshService:
    """Runs bounded, persisted fleet refreshes without blocking the web request."""

    MODE_NORMAL = "normal"
    MODE_FRESH_UPDATES = "fresh-updates"
    MODE_LEGACY_FULL = "full"
    MODE_USERS = "users"
    MODE_FRESH_USERS = "fresh-users"
    MODE_BACKUPS = "backups"
    MODE_FRESH_BACKUPS = "fresh-backups"
    SCHEDULED_REQUESTED_BY = "kosmos-scheduler"
    LEGACY_SCHEDULED_REQUESTED_BY = "kosmos-hub"
    BERLIN_TIMEZONE = ZoneInfo("Europe/Berlin")
    def __init__(self, *, db: Session):
        self.db = db

    def create_run(
        self,
        *,
        actor: HubUser,
        mode: str,
        site_ids: set[int] | None = None,
    ) -> tuple[FleetRefreshRun, bool]:
        if actor.role != "admin":
            raise ValueError("Only Hub administrators can start a fleet refresh.")
        if mode not in self.manual_modes():
            raise ValueError("Manual refreshes must use one of the available fresh checks.")

        active_run = self.db.scalar(
            select(FleetRefreshRun)
            .where(
                FleetRefreshRun.status.in_(
                    (
                        FleetRefreshRunStatus.queued.value,
                        FleetRefreshRunStatus.running.value,
                        FleetRefreshRunStatus.cancelling.value,
                    )
                )
            )
            .order_by(FleetRefreshRun.created_at.asc())
            .limit(1)
        )
        if active_run is not None:
            return active_run, False

        run = FleetRefreshRun(
            mode=mode,
            status=FleetRefreshRunStatus.queued.value,
            requested_by=actor.username,
            # Manual refreshes only read data; a Jet license is touched during a direct update.
            allow_provider_activation=False,
            result_json=self._initial_result(mode, target_site_ids=site_ids),
        )
        self.db.add(run)
        self.db.flush()
        return run, True

    def get_run(self, run_id: int) -> FleetRefreshRun | None:
        return self.db.get(FleetRefreshRun, run_id)

    def get_latest_run(self) -> FleetRefreshRun | None:
        return self.db.scalar(select(FleetRefreshRun).order_by(FleetRefreshRun.created_at.desc()).limit(1))

    def get_active_run(self, *, modes: set[str] | None = None) -> FleetRefreshRun | None:
        """Return the newest refresh that still needs progress polling."""
        statement = select(FleetRefreshRun).where(
            FleetRefreshRun.status.in_(
                (
                    FleetRefreshRunStatus.queued.value,
                    FleetRefreshRunStatus.running.value,
                    FleetRefreshRunStatus.cancelling.value,
                )
            )
        )
        if modes is not None:
            statement = statement.where(FleetRefreshRun.mode.in_(modes))
        return self.db.scalar(
            statement.order_by(FleetRefreshRun.created_at.desc()).limit(1)
        )

    def list_recent_runs(self, *, limit: int = 20, modes: set[str] | None = None) -> list[FleetRefreshRun]:
        statement = select(FleetRefreshRun)
        if modes is not None:
            statement = statement.where(FleetRefreshRun.mode.in_(modes))
        return list(
            self.db.scalars(
                statement.order_by(FleetRefreshRun.created_at.desc()).limit(limit)
            ).all()
        )

    def list_site_results(self, *, run_id: int) -> list[FleetRefreshSiteResult]:
        return list(
            self.db.scalars(
                select(FleetRefreshSiteResult)
                .where(FleetRefreshSiteResult.fleet_refresh_run_id == run_id)
                .order_by(FleetRefreshSiteResult.domain.asc())
            ).all()
        )

    def cancel_run(self, *, actor: HubUser, run_id: int) -> tuple[FleetRefreshRun, bool]:
        """Request a safe stop without interrupting an active WordPress request."""
        if actor.role != "admin":
            raise ValueError("Only Hub administrators can stop a fleet refresh.")

        run = self.db.get(FleetRefreshRun, run_id)
        if run is None:
            raise ValueError("The fleet refresh run no longer exists.")
        if run.status not in (FleetRefreshRunStatus.queued.value, FleetRefreshRunStatus.running.value):
            return run, False

        now = datetime.now(UTC)
        result = copy.deepcopy(run.result_json or {})
        result["cancellation"] = {
            "requested_at": now.isoformat(),
            "requested_by": actor.username,
        }
        run.result_json = result
        if run.status == FleetRefreshRunStatus.queued.value:
            run.status = FleetRefreshRunStatus.cancelled.value
            run.completed_at = now
        else:
            run.status = FleetRefreshRunStatus.cancelling.value
        write_audit_log(
            self.db,
            site=None,
            actor=actor.username,
            source="hub",
            action="fleet-refresh-cancel",
            result="requested",
            detail=f"Requested safe cancellation for fleet refresh run {run.id}.",
        )
        self.db.flush()
        return run, True

    @classmethod
    def queue_scheduled_run(cls, *, now: datetime | None = None) -> int | None:
        with SessionLocal() as db:
            settings_service = FleetRefreshSettingsService(db=db)
            now = now or datetime.now(UTC)
            runtime_settings = settings_service.ensure_auto_refresh_schedule(now=now)
            if not runtime_settings.auto_refresh_enabled:
                return None

            active_run = db.scalar(
                select(FleetRefreshRun)
                .where(
                    FleetRefreshRun.status.in_(
                        (
                            FleetRefreshRunStatus.queued.value,
                            FleetRefreshRunStatus.running.value,
                            FleetRefreshRunStatus.cancelling.value,
                        )
                    )
                )
                .order_by(FleetRefreshRun.created_at.asc())
                .limit(1)
            )
            if active_run is not None:
                return None

            if not cls._is_scheduled_run_due(runtime_settings=runtime_settings, now=now):
                return None

            # A scheduled cycle keeps its independent operational domains separate.
            # Runs are intentionally queued in this order because the worker is serial.
            runs: list[FleetRefreshRun] = []
            for mode in (cls.MODE_NORMAL, cls.MODE_USERS, cls.MODE_BACKUPS):
                run = FleetRefreshRun(
                    mode=mode,
                    status=FleetRefreshRunStatus.queued.value,
                    requested_by=cls.SCHEDULED_REQUESTED_BY,
                    allow_provider_activation=mode == cls.MODE_NORMAL,
                    result_json=cls._initial_result(mode),
                )
                db.add(run)
                runs.append(run)
            settings_service.advance_auto_refresh_schedule(now=now)
            db.commit()
            return runs[0].id

    @classmethod
    def _is_scheduled_run_due(
        cls,
        *,
        runtime_settings: FleetRefreshRuntimeSettings,
        now: datetime,
    ) -> bool:
        next_run_at = runtime_settings.auto_refresh_next_run_at
        if next_run_at is None:
            return False
        return cls._as_utc(now) >= cls._as_utc(next_run_at)

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
            db.execute(
                update(FleetRefreshRun)
                .where(FleetRefreshRun.status == FleetRefreshRunStatus.cancelling.value)
                .values(status=FleetRefreshRunStatus.cancelled.value, completed_at=datetime.now(UTC))
            )
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
            if cls._is_cancellation_requested(run_id):
                cls._finish_run(run_id, status=FleetRefreshRunStatus.cancelled.value, result=None, error_message=None)
                return
            cls._finish_run(run_id, status=FleetRefreshRunStatus.failed.value, result=None, error_message=str(exc))
            return

        if cls._is_cancellation_requested(run_id):
            cls._finish_run(run_id, status=FleetRefreshRunStatus.cancelled.value, result=result, error_message=None)
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
                "target_site_ids": cls._target_site_ids(run.result_json),
            }

    @classmethod
    def _perform_run(
        cls,
        *,
        run_id: int,
        mode: str,
        requested_by: str,
        allow_provider_activation: bool,
        target_site_ids: set[int] | None,
    ) -> dict[str, Any]:
        cls._validate_mode(mode)
        refresh_kind = cls._refresh_kind(mode)
        force = mode in (
            cls.MODE_FRESH_UPDATES,
            cls.MODE_FRESH_USERS,
            cls.MODE_FRESH_BACKUPS,
        )
        with SessionLocal() as db:
            runtime_settings = FleetRefreshSettingsService(db=db).get_runtime_settings()
        result = cls._initial_result(
            mode,
            runtime_settings=runtime_settings,
            target_site_ids=target_site_ids,
        )
        targets, cached_targets, skipped_targets = cls._site_targets(
            force=force,
            refresh_kind=refresh_kind,
            runtime_settings=runtime_settings,
            target_site_ids=target_site_ids,
        )
        result["sites"].update(
            {
                "total": len(targets) + len(cached_targets),
                "skipped": len(skipped_targets),
            }
        )
        result["backups"]["skipped"] = len(skipped_targets)
        result["users"]["skipped"] = len(skipped_targets)
        for target in cached_targets:
            outcome = cls._cached_site_outcome(target, refresh_kind=refresh_kind)
            cls._record_site_outcome(result, outcome)
            cls._store_site_result(run_id=run_id, outcome=outcome)
        for target in skipped_targets:
            cls._store_site_result(run_id=run_id, outcome=cls._skipped_site_outcome(target))
        result["phase"] = {
            "key": "site-checks",
            "label": {
                "updates": "Checking update status",
                "users": "Checking WordPress users",
                "backups": "Checking backup status",
            }[refresh_kind],
            "completed": result["sites"]["completed"],
            "total": result["sites"]["total"],
        }
        cls._store_progress(run_id, result)

        if targets and not cls._is_cancellation_requested(run_id):
            max_workers = min(runtime_settings.max_parallel_site_checks, len(targets))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                targets_iter = iter(targets)
                futures: dict[Any, dict[str, Any]] = {}
                for _ in range(max_workers):
                    try:
                        target = next(targets_iter)
                    except StopIteration:
                        break
                    futures[executor.submit(cls._refresh_one_site, target, refresh_kind=refresh_kind)] = target

                cancellation_requested = False
                while futures:
                    future = next(as_completed(futures))
                    target = futures.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = {
                            "site_id": target["site_id"],
                            "domain": target["domain"],
                            "state": "failed",
                            "updates": "skipped",
                            "backups": "skipped",
                            "users": "skipped",
                            "errors": [str(exc)],
                        }
                    cls._record_site_outcome(result, outcome)
                    cls._store_site_result(run_id=run_id, outcome=outcome)
                    result["phase"]["completed"] = result["sites"]["completed"]
                    cls._store_progress(run_id, result)

                    cancellation_requested = cancellation_requested or cls._is_cancellation_requested(run_id)
                    if cancellation_requested:
                        continue
                    try:
                        next_target = next(targets_iter)
                    except StopIteration:
                        continue
                    futures[executor.submit(cls._refresh_one_site, next_target, refresh_kind=refresh_kind)] = next_target

        if cls._is_cancellation_requested(run_id):
            return result
        if refresh_kind == "updates":
            cls._refresh_provider_evidence(
                run_id=run_id,
                result=result,
                requested_by=requested_by,
                allow_provider_activation=allow_provider_activation,
                target_site_ids=target_site_ids,
            )
        else:
            result["phase"] = {
                "key": "complete",
                "label": "Refresh completed",
                "completed": 1,
                "total": 1,
            }
            cls._store_progress(run_id, result)
        return result

    @classmethod
    def _site_targets(
        cls,
        *,
        force: bool,
        refresh_kind: str,
        runtime_settings: FleetRefreshRuntimeSettings,
        target_site_ids: set[int] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        with SessionLocal() as db:
            repository = SiteRepository(db)
            sites = repository.list_sites(limit=1000)
            if target_site_ids is not None:
                sites = [site for site in sites if site.id in target_site_ids]
            snapshots = repository.get_latest_snapshots_by_site_ids([site.id for site in sites])
            update_snapshots = repository.get_latest_update_snapshots_by_site_ids([site.id for site in sites])
            backup_snapshots = repository.get_latest_backup_snapshots_by_site_ids([site.id for site in sites])
            user_snapshots = repository.get_latest_user_snapshots_by_site_ids([site.id for site in sites])
            now = datetime.now(UTC)
            targets: list[dict[str, Any]] = []
            cached_targets: list[dict[str, Any]] = []
            skipped_targets: list[dict[str, Any]] = []
            for site in sites:
                if site.status != SiteStatus.verified.value:
                    skipped_targets.append(
                        {
                            "site_id": site.id,
                            "domain": site.domain,
                            "detail": f"Skipped because this site is {site.status or 'not verified'}.",
                        }
                    )
                    continue
                max_age = timedelta(minutes=runtime_settings.site_status_max_age_minutes)
                fresh_by_kind = {
                    "updates": cls._is_fresh(snapshots.get(site.id), now=now, max_age=max_age)
                    and cls._is_fresh(update_snapshots.get(site.id), now=now, max_age=max_age),
                    "backups": cls._is_fresh(backup_snapshots.get(site.id), now=now, max_age=max_age),
                    "users": cls._is_fresh(user_snapshots.get(site.id), now=now, max_age=max_age),
                }
                if not force and fresh_by_kind[refresh_kind]:
                    cached_targets.append(
                        {
                            "site_id": site.id,
                            "domain": site.domain,
                            "detail": f"The stored {refresh_kind} data was still fresh and was reused from the cache.",
                        }
                    )
                    continue
                targets.append({"site_id": site.id, "domain": site.domain})
            return targets, cached_targets, skipped_targets

    @classmethod
    def _refresh_one_site(cls, target: dict[str, Any], *, refresh_kind: str) -> dict[str, Any]:
        site_id = int(target["site_id"])
        with SessionLocal() as db:
            repository = SiteRepository(db)
            site = repository.get_site(site_id)
            if site is None:
                return {"site_id": site_id, "domain": target["domain"], "state": "failed", "updates": "skipped", "backups": "skipped", "users": "skipped", "errors": ["Site no longer exists."]}

            errors: list[str] = []
            if refresh_kind == "users":
                try:
                    inventory = SiteUserService(db=db, cipher=get_secret_cipher()).refresh_site_users(site_id)
                except SiteMcpProxyError as exc:
                    return {
                        "site_id": site_id,
                        "domain": site.domain,
                        "state": "refreshed",
                        "updates": "skipped",
                        "backups": "skipped",
                        "users": "failed",
                        "errors": [exc.message],
                    }
                return {
                    "site_id": site_id,
                    "domain": site.domain,
                    "state": "refreshed",
                    "updates": "skipped",
                    "backups": "skipped",
                    "users": "refreshed" if inventory.snapshot.available else "unsupported",
                    "errors": errors,
                }

            if refresh_kind == "backups":
                try:
                    SiteBackupService(db=db, cipher=get_secret_cipher()).refresh_site_backup_status(site_id)
                except SiteMcpProxyError as exc:
                    return {
                        "site_id": site_id,
                        "domain": site.domain,
                        "state": "refreshed",
                        "updates": "skipped",
                        "backups": "failed",
                        "users": "skipped",
                        "errors": [exc.message],
                    }
                return {
                    "site_id": site_id,
                    "domain": site.domain,
                    "state": "refreshed",
                    "updates": "skipped",
                    "backups": "refreshed",
                    "users": "skipped",
                    "errors": errors,
                }

            try:
                SiteInventoryService(db=db, cipher=get_secret_cipher()).refresh_site_state(site_id)
            except SiteMcpProxyError as exc:
                return {"site_id": site_id, "domain": site.domain, "state": "failed", "updates": "skipped", "backups": "skipped", "users": "skipped", "errors": [exc.message]}

            if not cls._bridge_supports_updates(site.bridge_version):
                errors.append("Bridge update support is unavailable.")
                return {"site_id": site_id, "domain": site.domain, "state": "refreshed", "updates": "skipped", "backups": "skipped", "users": "skipped", "errors": errors}

            try:
                SiteUpdateService(db=db, cipher=get_secret_cipher()).refresh_site_updates(site_id)
            except SiteMcpProxyError as exc:
                errors.append(exc.message)
                return {"site_id": site_id, "domain": site.domain, "state": "refreshed", "updates": "failed", "backups": "skipped", "users": "skipped", "errors": errors}
            return {"site_id": site_id, "domain": site.domain, "state": "refreshed", "updates": "refreshed", "backups": "skipped", "users": "skipped", "errors": errors}

    @classmethod
    def _record_site_outcome(cls, result: dict[str, Any], outcome: dict[str, Any]) -> None:
        result["sites"]["completed"] += 1
        result["last_site"] = outcome.get("domain", "")
        if outcome.get("state") == "refreshed":
            result["state"]["refreshed"] += 1
            result["sites"]["refreshed"] += 1
        elif outcome.get("state") == "cached":
            result["sites"]["cached"] += 1
        else:
            result["state"]["failed"] += 1
            result["sites"]["failed"] += 1
        if outcome.get("updates") == "refreshed":
            result["updates"]["refreshed"] += 1
        elif outcome.get("updates") == "failed":
            result["updates"]["failed"] += 1
        elif outcome.get("updates") == "cached":
            result["updates"]["cached"] += 1
        else:
            result["updates"]["skipped"] += 1
        if outcome.get("backups") == "refreshed":
            result["backups"]["refreshed"] += 1
        elif outcome.get("backups") == "failed":
            result["backups"]["failed"] += 1
        elif outcome.get("backups") == "cached":
            result["backups"]["cached"] += 1
        if outcome.get("users") == "refreshed":
            result["users"]["refreshed"] += 1
        elif outcome.get("users") == "failed":
            result["users"]["failed"] += 1
        elif outcome.get("users") == "unsupported":
            result["users"]["unsupported"] += 1
        elif outcome.get("users") == "cached":
            result["users"]["cached"] += 1
        for error in outcome.get("errors", []):
            result["errors"].append({"site": outcome.get("domain", "unknown"), "detail": str(error)[:240]})

    @staticmethod
    def _cached_site_outcome(target: dict[str, Any], *, refresh_kind: str) -> dict[str, Any]:
        return {
            "site_id": target["site_id"],
            "domain": target["domain"],
            "state": "cached",
            "updates": "cached" if refresh_kind == "updates" else "skipped",
            "backups": "cached" if refresh_kind == "backups" else "skipped",
            "users": "cached" if refresh_kind == "users" else "skipped",
            "detail": target["detail"],
            "errors": [],
        }

    @staticmethod
    def _skipped_site_outcome(target: dict[str, Any]) -> dict[str, Any]:
        return {
            "site_id": target["site_id"],
            "domain": target["domain"],
            "state": "skipped",
            "updates": "skipped",
            "backups": "skipped",
            "users": "skipped",
            "detail": target["detail"],
            "errors": [],
        }

    @classmethod
    def _store_site_result(cls, *, run_id: int, outcome: dict[str, Any]) -> None:
        site_id = int(outcome["site_id"])
        details = copy.deepcopy(outcome)
        errors = [str(error)[:240] for error in details.get("errors", [])]
        with SessionLocal() as db:
            record = db.scalar(
                select(FleetRefreshSiteResult).where(
                    FleetRefreshSiteResult.fleet_refresh_run_id == run_id,
                    FleetRefreshSiteResult.site_id == site_id,
                )
            )
            if record is None:
                record = FleetRefreshSiteResult(
                    fleet_refresh_run_id=run_id,
                    site_id=site_id,
                    domain=str(outcome.get("domain", "unknown")),
                    status=str(outcome.get("state", "failed")),
                    state_status=str(outcome.get("state", "failed")),
                    updates_status=str(outcome.get("updates", "skipped")),
                    backups_status=str(outcome.get("backups", "skipped")),
                    users_status=str(outcome.get("users", "skipped")),
                    jet_status="not-applicable",
                )
                db.add(record)
            else:
                record.domain = str(outcome.get("domain", record.domain))
                record.status = str(outcome.get("state", record.status))
                record.state_status = str(outcome.get("state", record.state_status))
                record.updates_status = str(outcome.get("updates", record.updates_status))
                record.backups_status = str(outcome.get("backups", record.backups_status))
                record.users_status = str(outcome.get("users", record.users_status))
            record.completed_at = datetime.now(UTC)
            record.detail = str(outcome.get("detail", "")) or None
            record.result_json = details
            record.error_message = "\n".join(errors) or None
            db.commit()

    @classmethod
    def _refresh_provider_evidence(
        cls,
        *,
        run_id: int,
        result: dict[str, Any],
        requested_by: str,
        allow_provider_activation: bool,
        target_site_ids: set[int] | None,
    ) -> None:
        if cls._is_cancellation_requested(run_id):
            return
        with SessionLocal() as db:
            inventory = FleetInventoryService(db=db, cipher=get_secret_cipher())
            items = inventory.list_items(limit=1000)
            if target_site_ids is not None:
                items = [item for item in items if item.site.id in target_site_ids]
            entries = inventory.build_update_workbench(items)
            version_service = OfficialPluginVersionService(db=db)
            jet_site_ids: set[int] = set()
            if allow_provider_activation:
                changelog = OfficialPluginVersionService.fetch_crocoblock_changelog_versions(
                    entry.identifier for entry in entries if entry.kind == "plugin"
                )
                result["crocoblock"].update(
                    {
                        "changelog_checked": changelog.requested,
                        "changelog_versions": len(changelog.versions),
                        "changelog_unavailable": changelog.requested - len(changelog.versions),
                        "changelog_error": changelog.error,
                    }
                )
                if changelog.versions:
                    result["crocoblock"]["changelog_catalog_versions"] = version_service.record_provider_versions(
                        (
                            {"plugin_file": plugin_file, "version": version}
                            for plugin_file, version in changelog.versions.items()
                        ),
                        source=OfficialPluginVersionService.CROCOBLOCK_CHANGELOG_SOURCE,
                    )
                jet_site_ids = cls._jet_sites_requiring_provider(
                    entries=entries,
                )
                cls._record_jet_license_candidates(
                    run_id=run_id,
                    entries=entries,
                    site_ids=jet_site_ids,
                )

            result["crocoblock"]["eligible"] = len(jet_site_ids)
            result["phase"] = {
                "key": "jet-catalogue",
                "label": "Checking Jet provider catalogues",
                "completed": 0,
                "total": len(jet_site_ids),
            }
            cls._store_progress(run_id, result)
            if jet_site_ids and allow_provider_activation:
                def report_crocoblock_progress(summary: dict[str, Any]) -> None:
                    result["crocoblock"].update(summary)
                    result["phase"]["completed"] = summary["completed"]
                    cls._store_progress(run_id, result)

                crocoblock = CrocoblockLicenseService(db=db, cipher=get_secret_cipher()).refresh_version_evidence(
                    actor=requested_by,
                    site_ids=jet_site_ids,
                    progress_callback=report_crocoblock_progress,
                    site_result_callback=lambda outcome: cls._store_jet_result(run_id=run_id, outcome=outcome),
                    should_cancel=lambda: cls._is_cancellation_requested(run_id),
                )
                result["crocoblock"].update(
                    {
                        "completed": crocoblock["completed"],
                        "refreshed": crocoblock["refreshed"],
                        "failed": crocoblock["failed"],
                        "catalog_versions": len(crocoblock["versions"]),
                    }
                )
                items = inventory.list_items(limit=1000)
                if target_site_ids is not None:
                    items = [item for item in items if item.site.id in target_site_ids]
            if cls._is_cancellation_requested(run_id):
                return
            result["phase"] = {
                "key": "official-versions",
                "label": "Checking official plugin catalogues",
                "completed": 0,
                "total": 0,
            }

            def report_official_progress(summary: dict[str, int]) -> None:
                result["official_versions"] = summary
                result["phase"].update({"completed": summary["completed"], "total": summary["checked"]})
                cls._store_progress(run_id, result)

            official = version_service.refresh_for_inventory(
                items,
                progress_callback=report_official_progress,
                should_cancel=lambda: cls._is_cancellation_requested(run_id),
            )
            if cls._is_cancellation_requested(run_id):
                return
            if jet_site_ids and allow_provider_activation:
                result["crocoblock"]["catalog_versions"] = version_service.record_provider_versions(
                    crocoblock["versions"],
                    source="Crocoblock Jet Dashboard",
                )
            result["official_versions"] = official
            result["phase"] = {
                "key": "diagnosis",
                "label": "Finishing the diagnosis",
                "completed": 0,
                "total": 1,
            }
            refreshed_items = inventory.list_items(limit=1000)
            if target_site_ids is not None:
                refreshed_items = [item for item in refreshed_items if item.site.id in target_site_ids]
            refreshed_entries = inventory.build_update_workbench(refreshed_items)
            result["mismatches"] = sum(1 for entry in refreshed_entries if entry.official_mismatch)
            result["phase"]["completed"] = 1
            write_audit_log(
                db,
                site=None,
                actor=requested_by,
                source="hub-worker",
                action="fleet-refresh",
                result="success",
                detail=(
                    f"Mode={result['mode']}; scope={result['scope']['label']}; refreshed {result['state']['refreshed']} site states and "
                    f"{result['updates']['refreshed']} update offers; diagnosed {result['mismatches']} mismatches."
                ),
            )
            db.commit()
        cls._store_progress(run_id, result)

    @classmethod
    def _jet_sites_requiring_provider(
        cls,
        *,
        entries: list[Any],
    ) -> set[int]:
        required_site_ids: set[int] = set()
        for entry in entries:
            if (
                entry.kind != "plugin"
                or not entry.identifier.startswith("jet-")
                or not entry.update_available
                or entry.execution_ready
            ):
                continue
            required_site_ids.add(entry.site.id)
        return required_site_ids

    @classmethod
    def _record_jet_license_candidates(
        cls,
        *,
        run_id: int,
        entries: list[Any],
        site_ids: set[int],
    ) -> None:
        plugins_by_site: dict[int, list[dict[str, str]]] = {}
        for entry in entries:
            if (
                entry.site.id not in site_ids
                or entry.kind != "plugin"
                or not entry.identifier.startswith("jet-")
                or not entry.update_available
                or entry.execution_ready
            ):
                continue
            plugins_by_site.setdefault(entry.site.id, []).append(
                {
                    "plugin_file": entry.identifier,
                    "name": entry.name,
                    "current_version": entry.current_version or "",
                    "target_version": entry.target_version or "",
                }
            )
        for site_id in site_ids:
            cls._store_jet_result(
                run_id=run_id,
                outcome={
                    "site_id": site_id,
                    "status": "license-check-queued",
                    "detail": "A Jet update is available but its authorized package is not ready; the stored Crocoblock license will be checked.",
                    "license_was_already_active": None,
                    "update_package_ready": None,
                    "provider_versions": [],
                    "dashboard_diagnostic": {},
                    "plugins": plugins_by_site.get(site_id, []),
                },
            )

    @classmethod
    def _store_jet_result(cls, *, run_id: int, outcome: dict[str, Any]) -> None:
        site_id = int(outcome["site_id"])
        with SessionLocal() as db:
            record = db.scalar(
                select(FleetRefreshSiteResult).where(
                    FleetRefreshSiteResult.fleet_refresh_run_id == run_id,
                    FleetRefreshSiteResult.site_id == site_id,
                )
            )
            if record is None:
                return
            result = copy.deepcopy(record.result_json or {})
            previous_jet = result.get("jet") if isinstance(result.get("jet"), dict) else {}
            plugins = outcome.get("plugins") or previous_jet.get("plugins") or []
            result["jet"] = {
                "status": str(outcome["status"]),
                "detail": str(outcome["detail"]),
                "license_was_already_active": outcome.get("license_was_already_active"),
                "update_package_ready": outcome.get("update_package_ready"),
                "plugins": plugins,
                "provider_versions": outcome.get("provider_versions", []),
                "dashboard_diagnostic": outcome.get("dashboard_diagnostic", {}),
            }
            record.jet_status = str(outcome["status"])
            record.result_json = result
            db.commit()

    @classmethod
    def _store_progress(cls, run_id: int, result: dict[str, Any]) -> None:
        with SessionLocal() as db:
            run = db.get(FleetRefreshRun, run_id)
            if run is not None and run.status in (
                FleetRefreshRunStatus.running.value,
                FleetRefreshRunStatus.cancelling.value,
            ):
                stored_result = copy.deepcopy(result)
                cancellation = (run.result_json or {}).get("cancellation")
                if cancellation:
                    stored_result["cancellation"] = cancellation
                run.result_json = stored_result
                db.commit()

    @classmethod
    def _is_cancellation_requested(cls, run_id: int) -> bool:
        with SessionLocal() as db:
            status = db.scalar(select(FleetRefreshRun.status).where(FleetRefreshRun.id == run_id))
            return status == FleetRefreshRunStatus.cancelling.value

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
        target_site_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        runtime_settings = runtime_settings or FleetRefreshRuntimeSettings()
        selected_site_ids = sorted(target_site_ids or [])
        return {
            "mode": mode,
            "scope": {
                "kind": "all" if target_site_ids is None else "selected",
                "site_ids": selected_site_ids,
                "count": len(selected_site_ids),
                "label": "All sites" if target_site_ids is None else f"{len(selected_site_ids)} selected site(s)",
            },
            "settings": {
                "site_status_max_age_minutes": runtime_settings.site_status_max_age_minutes,
                "max_parallel_site_checks": runtime_settings.max_parallel_site_checks,
            },
            "sites": {"total": 0, "completed": 0, "refreshed": 0, "cached": 0, "failed": 0, "skipped": 0},
            "state": {"refreshed": 0, "failed": 0},
            "updates": {"refreshed": 0, "failed": 0, "cached": 0, "skipped": 0},
            "backups": {"refreshed": 0, "failed": 0, "cached": 0, "skipped": 0},
            "users": {"refreshed": 0, "failed": 0, "cached": 0, "skipped": 0, "unsupported": 0},
            "crocoblock": {
                "eligible": 0,
                "completed": 0,
                "refreshed": 0,
                "cached": 0,
                "failed": 0,
                "catalog_versions": 0,
                "changelog_checked": 0,
                "changelog_versions": 0,
                "changelog_catalog_versions": 0,
                "changelog_unavailable": 0,
                "changelog_error": None,
            },
            "official_versions": {"total": 0, "checked": 0, "completed": 0, "cached": 0, "wordpress_org": 0, "elementor_pro": 0, "pafe_pro": 0, "provider_offer": 0, "unavailable": 0, "failed": 0},
            "phase": {"key": "queued", "label": "Waiting for a background worker", "completed": 0, "total": 0},
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
    def _target_site_ids(result: dict[str, Any] | None) -> set[int] | None:
        scope = (result or {}).get("scope", {})
        if scope.get("kind") != "selected":
            return None
        return {
            int(site_id)
            for site_id in scope.get("site_ids", [])
            if str(site_id).strip().isdigit()
        }

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
    def update_modes(cls) -> set[str]:
        return {cls.MODE_NORMAL, cls.MODE_FRESH_UPDATES}

    @classmethod
    def update_history_modes(cls) -> set[str]:
        return cls.update_modes() | {cls.MODE_LEGACY_FULL}

    @classmethod
    def manual_modes(cls) -> set[str]:
        return {cls.MODE_FRESH_UPDATES, cls.MODE_FRESH_USERS, cls.MODE_FRESH_BACKUPS}

    @classmethod
    def user_modes(cls) -> set[str]:
        return {cls.MODE_USERS, cls.MODE_FRESH_USERS}

    @classmethod
    def backup_modes(cls) -> set[str]:
        return {cls.MODE_BACKUPS, cls.MODE_FRESH_BACKUPS}

    @classmethod
    def _refresh_kind(cls, mode: str) -> str:
        if mode in cls.update_modes():
            return "updates"
        if mode in cls.user_modes():
            return "users"
        if mode in cls.backup_modes():
            return "backups"
        raise ValueError(f"Unknown refresh mode: {mode}")

    @classmethod
    def _validate_mode(cls, mode: str) -> None:
        if mode not in cls.update_modes() | cls.user_modes() | cls.backup_modes():
            raise ValueError("Refresh mode must be an updates, users, or backups mode.")
