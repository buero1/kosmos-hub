import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import SecretCipher
from app.core.timezones import BERLIN_TIMEZONE
from app.models.maintenance_run import (
    MaintenanceRun,
    MaintenanceRunStatus,
    MaintenanceRunStep,
    MaintenanceRunStepStatus,
)
from app.models.plugin_installation_package import PluginInstallationPackage
from app.models.site import Site, SiteStatus
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.crocoblock_license import CrocoblockLicenseError, CrocoblockLicenseService
from app.services.fleet_inventory import FleetInventoryService, UpdateWorkbenchEntry
from app.services.site_backups import SiteBackupService
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService
from app.services.site_inventory import SiteInventoryService
from app.services.site_updates import SiteUpdateService


@dataclass(frozen=True)
class MaintenanceRunOutcome:
    run: MaintenanceRun
    result: str
    message: str


@dataclass(frozen=True)
class PluginUpdateBatchOutcome:
    batch_id: str
    run_count: int
    message: str


@dataclass(frozen=True)
class DirectUpdateBatchCancellationOutcome:
    batch_id: str
    cancelled_queued_runs: int
    processing_runs: int
    cancellation_requested: bool


@dataclass(frozen=True)
class SiteMaintenanceHistoryDay:
    key: str
    label: str
    runs: list[MaintenanceRun]
    workflow_child_runs: dict[int, list[MaintenanceRun]]
    run_count: int


class MaintenanceRunService:
    """Run bounded maintenance tasks and persist evidence for later automation."""

    UPDRAFT_BACKUP_KIND = "updraftplus-backup"
    UPDRAFT_BACKUP_DELETE_KIND = "updraftplus-backup-delete"
    PLUGIN_UPDATE_KIND = "direct-plugin-update"
    PLUGIN_INSTALLATION_KIND = "plugin-installation"
    COMPLETE_SITE_UPDATE_KIND = "complete-site-update"
    DIRECT_UPDATE_FAILURE_STREAK_LIMIT = 5
    LARGE_DIRECT_PLUGIN_BATCH_THRESHOLD = 100
    COMPLETE_SITE_UPDATE_MAX_WAVES = 3
    FINAL_BRIDGE_PREFLIGHT_MIN_VERSION = "0.3.59"
    FINAL_BRIDGE_PREFLIGHT_RETRY_LIMIT = 2
    START_BACKUP_ABILITY = "kosmos-bridge/start-updraftplus-backup"
    BACKUP_STATUS_ABILITY = "kosmos-bridge/get-updraftplus-backup-status"
    LIST_BACKUPS_ABILITY = "kosmos-bridge/list-updraftplus-backups"
    DELETE_BACKUP_ABILITY = "kosmos-bridge/delete-updraftplus-backup"
    VERIFY_BACKUP_DELETION_ABILITY = "kosmos-bridge/verify-updraftplus-backup-deletion"
    LIST_INSTALLED_PLUGINS_ABILITY = "kosmos-bridge/list-installed-plugins"
    PLUGIN_UPDATE_ABILITY = "kosmos-bridge/update-plugin"
    PLUGIN_ACTIVATION_ABILITY = "kosmos-bridge/activate-plugin"
    PLUGIN_INSTALLATION_ABILITY = "kosmos-bridge/install-plugin"
    THEME_UPDATE_ABILITY = "kosmos-bridge/update-theme"
    WORDPRESS_CORE_UPDATE_ABILITY = "kosmos-bridge/update-wordpress-core"
    SITE_HEALTH_ABILITY = "kosmos-bridge/check-site-health"
    START_BACKUP_TIMEOUT_SECONDS = 20
    DELETE_BACKUP_TIMEOUT_SECONDS = 180
    REMOTE_DELETION_VERIFICATION_TIMEOUT_SECONDS = 60
    BACKUP_TIMEOUT = timedelta(minutes=3)
    POST_UPDATE_HEALTH_MAX_ATTEMPTS = 3
    POST_UPDATE_HEALTH_RETRY_DELAY_SECONDS = 10
    POST_UPDATE_HEALTH_STALE_RECOVERY_AFTER = timedelta(minutes=2)
    POST_UPDATE_FRAMEWORK_STABILIZATION_SECONDS = 5
    POST_UPDATE_FRAMEWORK_PLUGIN_IDENTIFIERS = frozenset(
        {
            "elementor/elementor.php",
            "elementor-pro/elementor-pro.php",
            "jet-engine/jet-engine.php",
        }
    )

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    def list_site_runs(self, site_id: int, *, limit: int | None = None) -> list[MaintenanceRun]:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps))
            .where(MaintenanceRun.site_id == site_id)
            .order_by(MaintenanceRun.started_at.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.db.scalars(statement))

    def list_site_run_history(self, site_id: int) -> list[SiteMaintenanceHistoryDay]:
        """Return complete history grouped by day, keeping workflow children under their parent."""
        all_runs = self.list_site_runs(site_id)
        runs_by_id = {run.id: run for run in all_runs}
        child_runs_by_workflow_run_id: dict[int, list[MaintenanceRun]] = defaultdict(list)
        workflow_child_run_ids: set[int] = set()

        for run in all_runs:
            workflow_run_id = (run.result_json or {}).get("workflow_run_id")
            workflow_run = runs_by_id.get(workflow_run_id) if isinstance(workflow_run_id, int) else None
            if workflow_run is None or workflow_run.kind != self.COMPLETE_SITE_UPDATE_KIND:
                continue
            child_runs_by_workflow_run_id[workflow_run_id].append(run)
            workflow_child_run_ids.add(run.id)

        for child_runs in child_runs_by_workflow_run_id.values():
            child_runs.sort(key=lambda run: (self._as_utc(run.started_at), run.id))

        history: list[SiteMaintenanceHistoryDay] = []
        current_day_key: str | None = None
        current_day_runs: list[MaintenanceRun] = []

        for run in all_runs:
            if run.id in workflow_child_run_ids:
                continue
            local_started_at = self._as_utc(run.started_at).astimezone(BERLIN_TIMEZONE)
            day_key = local_started_at.strftime("%Y-%m-%d")
            if current_day_key is None:
                current_day_key = day_key
            if day_key != current_day_key:
                history.append(
                    SiteMaintenanceHistoryDay(
                        key=current_day_key,
                        label=self._maintenance_history_day_label(current_day_key),
                        runs=current_day_runs,
                        workflow_child_runs={
                            run.id: child_runs_by_workflow_run_id[run.id]
                            for run in current_day_runs
                            if run.id in child_runs_by_workflow_run_id
                        },
                        run_count=sum(
                            1 + len(child_runs_by_workflow_run_id.get(run.id, []))
                            for run in current_day_runs
                        ),
                    )
                )
                current_day_key = day_key
                current_day_runs = []
            current_day_runs.append(run)

        if current_day_key is not None:
            history.append(
                SiteMaintenanceHistoryDay(
                    key=current_day_key,
                    label=self._maintenance_history_day_label(current_day_key),
                    runs=current_day_runs,
                    workflow_child_runs={
                        run.id: child_runs_by_workflow_run_id[run.id]
                        for run in current_day_runs
                        if run.id in child_runs_by_workflow_run_id
                    },
                    run_count=sum(
                        1 + len(child_runs_by_workflow_run_id.get(run.id, []))
                        for run in current_day_runs
                    ),
                )
            )
        return history

    def recover_stale_direct_update_postflights(self, *, limit: int = 25) -> dict[str, int]:
        """Finish an interrupted post-update health check without running the update again."""
        outcomes = {"succeeded": 0, "failed": 0, "waiting": 0}
        if limit < 1:
            return outcomes

        stale_before = datetime.now(UTC) - self.POST_UPDATE_HEALTH_STALE_RECOVERY_AFTER
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(
                MaintenanceRun.kind == self.PLUGIN_UPDATE_KIND,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.last_checked_at.asc(), MaintenanceRun.id.asc())
        )
        stale_runs = [
            run
            for run in self.db.scalars(statement)
            if (run.result_json or {}).get("stage") == "postflight-health"
            and run.last_checked_at is not None
            and self._as_utc(run.last_checked_at) <= stale_before
        ][:limit]

        for run in stale_runs:
            details = self._direct_update_details(run)
            update_step = self._find_step(run, self._update_step_key(details["update_kind"])) if details else None
            update_result = dict(update_step.result_json or {}) if update_step is not None else {}
            if details is None or self._direct_update_result_error(details, update_result):
                outcomes["waiting"] += 1
                continue

            health_step = self._find_step(run, "postflight-health")
            self._start_plugin_update_step(
                run,
                health_step,
                "Resuming the post-update health check after an interrupted Hub worker.",
            )
            health_error, health_detail, health_result = self._run_direct_update_postflight_health(run, health_step)
            if health_error:
                run.result_json = {**(run.result_json or {}), "post_update_health": health_result}
                self._fail_plugin_update_run(
                    run,
                    f"{details['update_name']} was updated and verified, but {health_error}. No automatic rollback was performed.",
                    health_step=health_step,
                )
                outcomes["failed"] += 1
                continue

            self._complete_confirmed_direct_update_run(
                run,
                details,
                update_result,
                health_step,
                health_detail,
                health_result,
                recovery_note="An interrupted post-update health check was resumed without repeating the update.",
            )
            outcomes["succeeded"] += 1
        return outcomes

    def reconcile_admin_ajax_access_denied_batch(self, batch_id: str) -> int:
        """Repair confirmed updates that an access policy misclassified as failed."""
        reconciled = 0
        for run in self._direct_update_batch_runs(batch_id):
            health_result = (run.result_json or {}).get("post_update_health")
            if run.status != MaintenanceRunStatus.failed.value or not self._admin_ajax_access_is_ignored(health_result):
                continue

            details = self._direct_update_details(run)
            update_step = self._find_step(run, self._update_step_key(details["update_kind"])) if details else None
            update_result = dict(update_step.result_json or {}) if update_step is not None else {}
            if details is None or self._direct_update_result_error(details, update_result):
                continue

            health_step = self._find_step(run, "postflight-health")
            self._complete_confirmed_direct_update_run(
                run,
                details,
                update_result,
                health_step,
                self._plugin_update_health_detail(health_result),
                health_result,
                recovery_note="The admin AJAX access policy returned HTTP 403 and was recorded as a non-blocking warning.",
            )
            reconciled += 1
        return reconciled

    def start_updraftplus_backup(
        self,
        *,
        site_id: int,
        actor: str,
        cleanup_oldest: bool = True,
    ) -> MaintenanceRunOutcome:
        site = self.repository.get_site(site_id)
        if site is None:
            raise ValueError("Site not found.")
        if site.status != SiteStatus.verified.value:
            raise ValueError("Only verified sites can start a maintenance run.")

        active_run = self.db.scalar(
            select(MaintenanceRun)
            .where(
                MaintenanceRun.site_id == site_id,
                MaintenanceRun.kind.in_((self.UPDRAFT_BACKUP_KIND, self.UPDRAFT_BACKUP_DELETE_KIND)),
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.desc())
            .limit(1)
        )
        if active_run is not None:
            return MaintenanceRunOutcome(
                run=active_run,
                result="blocked",
                message="An UpdraftPlus backup action is already running for this site.",
            )

        now = datetime.now(UTC)
        run = MaintenanceRun(
            site=site,
            kind=self.UPDRAFT_BACKUP_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by=actor,
            started_at=now,
            result_json={"cleanup_oldest": cleanup_oldest},
        )
        request_step = MaintenanceRunStep(
            run=run,
            step_key="request-backup",
            status=MaintenanceRunStepStatus.running.value,
            started_at=now,
            detail="Requesting a new full backup from UpdraftPlus.",
            result_json={},
        )
        self.db.add_all((run, request_step))
        self.db.flush()

        try:
            payload = self.proxy.execute_ability(
                site_id,
                self.START_BACKUP_ABILITY,
                {},
                timeout_seconds=self.START_BACKUP_TIMEOUT_SECONDS,
            )
        except SiteMcpProxyError as exc:
            self._fail_run(run, actor=actor, message=exc.message)
            return MaintenanceRunOutcome(run=run, result="failed", message=exc.message)

        result = self._result_from_payload(payload)
        backup_nonce = result.get("backup_nonce")
        if (
            result.get("accepted") is not True
            or result.get("retention_protection_requested") is not True
            or not self._is_backup_nonce(backup_nonce)
        ):
            message = self._safe_message(
                result.get("message"),
                "UpdraftPlus did not accept a protected backup request.",
            )
            self._fail_run(run, actor=actor, message=message)
            return MaintenanceRunOutcome(run=run, result="failed", message=message)

        run.bridge_backup_nonce = backup_nonce
        bridge_status = self._bridge_backup_status(result)
        bridge_message = self._bridge_backup_message(result, bridge_status)
        run.result_json = {
            "provider": "updraftplus",
            "backup_nonce": backup_nonce,
            "retention_protection_requested": True,
            "cleanup_oldest": cleanup_oldest,
            "bridge_status": bridge_status,
            "bridge_status_message": bridge_message,
            "scheduled_at": self._safe_string(result.get("scheduled_at")),
        }
        request_step.status = MaintenanceRunStepStatus.succeeded.value
        request_step.completed_at = datetime.now(UTC)
        request_step.detail = bridge_message
        request_step.result_json = dict(run.result_json)
        self.db.add(
            MaintenanceRunStep(
                run=run,
                step_key="verify-backup",
                status=MaintenanceRunStepStatus.waiting.value,
                started_at=datetime.now(UTC),
                detail=self._backup_waiting_detail(bridge_status, bridge_message),
                result_json={},
            )
        )
        write_audit_log(
            self.db,
            site=site,
            actor=actor,
            source="hub-web",
            action="start-updraftplus-backup-run",
            result="running",
            detail=f"Started maintenance run {run.id} for a fresh UpdraftPlus backup.",
        )
        self.db.commit()
        self.db.refresh(run)
        return MaintenanceRunOutcome(
            run=run,
            result="started",
            message=(
                "The protected UpdraftPlus backup was queued. The Hub will verify it and remove the oldest eligible backup afterwards."
                if cleanup_oldest
                else "The protected UpdraftPlus backup was queued. The Hub will verify it without automatic cleanup."
            ),
        )

    def start_updraftplus_backup_deletion(
        self,
        *,
        site_id: int,
        selections: list[str],
        actor: str,
    ) -> MaintenanceRunOutcome:
        site = self.repository.get_site(site_id)
        if site is None:
            raise ValueError("Site not found.")
        if site.status != SiteStatus.verified.value:
            raise ValueError("Only verified sites can start a maintenance run.")

        active_run = self.db.scalar(
            select(MaintenanceRun)
            .where(
                MaintenanceRun.site_id == site_id,
                MaintenanceRun.kind.in_((self.UPDRAFT_BACKUP_KIND, self.UPDRAFT_BACKUP_DELETE_KIND)),
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.desc())
            .limit(1)
        )
        if active_run is not None:
            return MaintenanceRunOutcome(
                run=active_run,
                result="blocked",
                message="An UpdraftPlus backup action is already running for this site.",
            )

        selected_identities = self._selected_backup_identities(selections)
        if not selected_identities:
            raise ValueError("Select at least one backup before deleting it.")

        try:
            payload = self.proxy.execute_readonly_ability(
                site_id,
                self.LIST_BACKUPS_ABILITY,
                {},
                timeout_seconds=30,
            )
        except SiteMcpProxyError as exc:
            raise ValueError(f"Backups could not be checked before deletion: {exc.message}") from exc

        backup_targets = self._selected_backup_targets(
            self._result_from_payload(payload).get("backups"),
            selected_identities,
        )
        if len(backup_targets) != len(selected_identities):
            raise ValueError("One or more selected backups no longer match the current UpdraftPlus history. Check backups and try again.")

        now = datetime.now(UTC)
        run = MaintenanceRun(
            site=site,
            kind=self.UPDRAFT_BACKUP_DELETE_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by=actor,
            started_at=now,
            result_json={"operation": "delete-selected", "target_count": len(backup_targets)},
        )
        self.db.add(run)
        self.db.flush()
        for index, target in enumerate(backup_targets, start=1):
            self.db.add(
                MaintenanceRunStep(
                    run=run,
                    step_key=f"delete-backup-{index}",
                    status=MaintenanceRunStepStatus.waiting.value,
                    started_at=now,
                    detail=f"Queued deletion of the backup from {target['backup_at']}.",
                    result_json=target,
                )
            )
        write_audit_log(
            self.db,
            site=site,
            actor=actor,
            source="hub-web",
            action="start-updraftplus-backup-deletion-run",
            result="running",
            detail=f"Queued deletion of {len(backup_targets)} selected UpdraftPlus backup set(s).",
        )
        self.db.commit()
        self.db.refresh(run)
        return MaintenanceRunOutcome(
            run=run,
            result="started",
            message=f"The deletion of {len(backup_targets)} selected backup set(s) was queued and will be verified against remote storage.",
        )

    def start_direct_updates(
        self,
        *,
        selected_keys: list[str],
        actor: str,
        expected_site_id: int | None = None,
        large_batch_confirmation: str = "",
    ) -> PluginUpdateBatchOutcome:
        """Queue selected WordPress, theme, or plugin updates without review plans."""
        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        entries = inventory.build_update_workbench(inventory.list_items(limit=1000))
        entries_by_key = {entry.plan_key: entry for entry in entries}
        requested_keys = list(dict.fromkeys(key for key in selected_keys if key))

        if not requested_keys:
            raise ValueError("Select at least one update before starting the run.")
        if any(key not in entries_by_key for key in requested_keys):
            raise ValueError("One or more selected updates are no longer available. Refresh the workbench and try again.")

        selected_entries = [entries_by_key[key] for key in requested_keys]
        scope_error = self._selection_scope_error(selected_entries, expected_site_id=expected_site_id)
        if scope_error:
            raise ValueError(scope_error)
        confirmation_error = self._large_direct_plugin_batch_confirmation_error(
            selected_entries,
            large_batch_confirmation,
        )
        if confirmation_error:
            raise ValueError(confirmation_error)
        has_stored_crocoblock_license = self._has_stored_crocoblock_license()
        for entry in selected_entries:
            scope_error = self._direct_plugin_update_scope_error(
                entry,
                allow_stored_crocoblock_license=True,
                has_stored_crocoblock_license=has_stored_crocoblock_license,
            )
            if scope_error:
                raise ValueError(scope_error)

        selected_site_ids = {entry.site.id for entry in selected_entries}
        active_runs = self.db.scalars(
            select(MaintenanceRun).where(
                MaintenanceRun.kind.in_((self.PLUGIN_UPDATE_KIND, self.COMPLETE_SITE_UPDATE_KIND)),
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
                MaintenanceRun.site_id.in_(selected_site_ids),
            )
        )
        if any(active_runs):
            raise ValueError("A direct update is already queued or running for one of the selected sites.")

        batch_id = uuid4().hex
        now = datetime.now(UTC)
        runs: list[MaintenanceRun] = []
        for position, entry in enumerate(selected_entries, start=1):
            update_step_key = self._update_step_key(entry.kind)
            run = MaintenanceRun(
                site=entry.site,
                kind=self.PLUGIN_UPDATE_KIND,
                status=MaintenanceRunStatus.running.value,
                requested_by=actor,
                started_at=now,
                result_json={
                    "batch_id": batch_id,
                    "batch_position": position,
                    "batch_size": len(selected_entries),
                    "update_kind": entry.kind,
                    "update_identifier": entry.identifier,
                    "update_name": entry.name,
                    "current_version": entry.current_version,
                    "target_version": entry.target_version,
                    "expected_active": entry.is_active,
                    "stage": "queued",
                    "stage_message": "Queued for direct update with a final on-site Bridge preflight.",
                },
            )
            run.steps.extend(
                (
                    MaintenanceRunStep(
                        step_key="preflight",
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting for the final on-site Bridge update check.",
                        result_json={},
                    ),
                    MaintenanceRunStep(
                        step_key=update_step_key,
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting for the preflight to pass.",
                        result_json={},
                    ),
                    MaintenanceRunStep(
                        step_key="postflight-health",
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting for the selected update to complete.",
                        result_json={},
                    ),
                )
            )
            self.db.add(run)
            runs.append(run)

        self.db.flush()
        for run in runs:
            write_audit_log(
                self.db,
                site=run.site,
                actor=actor,
                source="hub-web",
                action="start-direct-update-run",
                result="queued",
                detail=(
                    f"Queued direct {run.result_json['update_kind']} update run {run.id} in batch {batch_id[:12]} for "
                    f"{run.result_json['update_name']} {run.result_json['current_version']} -> "
                    f"{run.result_json['target_version']}."
                ),
                request_id=batch_id,
            )
        self.db.commit()
        return PluginUpdateBatchOutcome(
            batch_id=batch_id,
            run_count=len(runs),
            message=(
                f"Queued {len(runs)} direct update{'s' if len(runs) != 1 else ''}. "
                "Each update will recheck the current available version and run a health check afterwards."
            ),
        )

    def start_complete_site_update(self, *, site_id: int, actor: str) -> MaintenanceRunOutcome:
        """Queue one website for a fresh, ordered WordPress/theme/plugin update workflow."""
        site = self.repository.get_site(site_id)
        if site is None:
            raise ValueError("Site not found.")
        if site.status != SiteStatus.verified.value:
            raise ValueError("Only verified sites can start a complete update workflow.")

        active_run = self.db.scalar(
            select(MaintenanceRun)
            .where(
                MaintenanceRun.site_id == site_id,
                MaintenanceRun.kind.in_(
                    (
                        self.PLUGIN_UPDATE_KIND,
                        self.PLUGIN_INSTALLATION_KIND,
                        self.COMPLETE_SITE_UPDATE_KIND,
                    )
                ),
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.desc())
            .limit(1)
        )
        if active_run is not None:
            raise ValueError("This website already has queued or running maintenance.")

        now = datetime.now(UTC)
        run = MaintenanceRun(
            site=site,
            kind=self.COMPLETE_SITE_UPDATE_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by=actor,
            started_at=now,
            result_json={
                "stage": "queued",
                "stage_message": "Queued for a fresh WordPress, theme, and plugin update workflow.",
                "workflow_phase": "queued",
                "wave": 0,
                "max_waves": self.COMPLETE_SITE_UPDATE_MAX_WAVES,
                "successful_updates": 0,
                "failed_updates": 0,
                "skipped_updates": 0,
                "events": [],
            },
        )
        run.steps.append(
            MaintenanceRunStep(
                step_key="workflow",
                status=MaintenanceRunStepStatus.waiting.value,
                started_at=now,
                detail="Waiting to start the full website update workflow.",
                result_json={},
            )
        )
        self.db.add(run)
        self.db.flush()
        write_audit_log(
            self.db,
            site=site,
            actor=actor,
            source="hub-web",
            action="start-complete-site-update-run",
            result="queued",
            detail=(
                f"Queued complete site update run {run.id} for {site.domain}: "
                "WordPress, themes, plugins, and fresh follow-up checks."
            ),
            request_id=str(run.id),
        )
        self.db.commit()
        return MaintenanceRunOutcome(
            run=run,
            result="started",
            message=(
                f"The complete update workflow for {site.domain} was queued. "
                "Live progress is shown below."
            ),
        )

    def get_complete_site_update_run(self, run_id: int) -> MaintenanceRun | None:
        return self.db.scalar(
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(
                MaintenanceRun.id == run_id,
                MaintenanceRun.kind == self.COMPLETE_SITE_UPDATE_KIND,
            )
        )

    def complete_site_update_child_runs(self, workflow_run_id: int) -> list[MaintenanceRun]:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.site))
            .where(MaintenanceRun.kind == self.PLUGIN_UPDATE_KIND)
            .order_by(MaintenanceRun.id.asc())
        )
        return [
            run
            for run in self.db.scalars(statement)
            if isinstance(run.result_json, dict) and run.result_json.get("workflow_run_id") == workflow_run_id
        ]

    def next_complete_site_update_run_ids(self, *, limit: int = 1) -> list[int]:
        """Return queued complete-site workflows; one site is intentionally serial for now."""
        if limit < 1:
            return []
        stale_before = datetime.now(UTC) - timedelta(minutes=10)
        statement = (
            select(MaintenanceRun)
            .where(
                MaintenanceRun.kind == self.COMPLETE_SITE_UPDATE_KIND,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.asc(), MaintenanceRun.id.asc())
        )
        run_ids: list[int] = []
        for run in self.db.scalars(statement):
            details = run.result_json if isinstance(run.result_json, dict) else {}
            if details.get("workflow_run_id"):
                # Complete-site workflows execute their child updates synchronously themselves.
                continue
            stage = details.get("stage", "queued")
            stale_processing = (
                stage == "processing"
                and (run.last_checked_at is None or self._as_utc(run.last_checked_at) <= stale_before)
            )
            if stage != "queued" and not stale_processing:
                continue
            run_ids.append(run.id)
            if len(run_ids) >= limit:
                break
        return run_ids

    def cancel_complete_site_update(self, *, run_id: int, actor: str) -> bool:
        run = self.get_complete_site_update_run(run_id)
        if run is None:
            raise ValueError("The complete update workflow no longer exists.")
        if run.status != MaintenanceRunStatus.running.value:
            return False
        result = dict(run.result_json or {})
        if isinstance(result.get("cancellation"), dict):
            return False
        result["cancellation"] = {
            "requested_by": actor,
            "requested_at": datetime.now(UTC).isoformat(),
        }
        result["stage_message"] = "Cancellation was requested. The current update will finish safely."
        run.result_json = result
        run.last_checked_at = datetime.now(UTC)
        write_audit_log(
            self.db,
            site=run.site,
            actor=actor,
            source="hub-web",
            action="cancel-complete-site-update-run",
            result="requested",
            detail=f"Cancellation requested for complete site update run {run.id}.",
            request_id=str(run.id),
        )
        self.db.commit()
        return True

    def start_plugin_updates(
        self,
        *,
        selected_keys: list[str],
        actor: str,
    ) -> PluginUpdateBatchOutcome:
        """Backward-compatible name for starting updates from the global workbench."""
        return self.start_direct_updates(selected_keys=selected_keys, actor=actor)

    @classmethod
    def _large_direct_plugin_batch_confirmation_error(
        cls,
        entries: list[UpdateWorkbenchEntry],
        confirmation: str,
    ) -> str | None:
        plugin_count = sum(entry.kind == "plugin" for entry in entries)
        if plugin_count <= cls.LARGE_DIRECT_PLUGIN_BATCH_THRESHOLD:
            return None
        if confirmation.strip().casefold() == "update":
            return None
        return (
            f"Type update to confirm more than {cls.LARGE_DIRECT_PLUGIN_BATCH_THRESHOLD} selected plugin updates."
        )

    def start_plugin_installations(
        self,
        *,
        site_ids: list[int],
        package: PluginInstallationPackage,
        activate: bool,
        replace_existing: bool,
        actor: str,
    ) -> PluginUpdateBatchOutcome:
        """Queue one Hub-inspected plugin package for verified customer sites."""
        requested_site_ids = list(dict.fromkeys(site_id for site_id in site_ids if isinstance(site_id, int)))
        if not requested_site_ids:
            raise ValueError("Select at least one website before installing a plugin.")
        if not self._is_plugin_file(package.plugin_file) or not package.plugin_version or not re.fullmatch(r"[a-f0-9]{64}", package.sha256):
            raise ValueError("The checked plugin package has invalid installation metadata.")

        sites: list[Site] = []
        for site_id in requested_site_ids:
            site = self.repository.get_site(site_id)
            if site is None:
                raise ValueError("One or more selected websites no longer exist.")
            if site.status != SiteStatus.verified.value:
                raise ValueError(f"{site.domain} is not verified for plugin installation.")
            ability_names = {capability.ability_name for capability in site.capabilities}
            if self.PLUGIN_INSTALLATION_ABILITY not in ability_names:
                raise ValueError(
                    f"{site.domain} does not yet support plugin installation. Update Kosmos Bridge to version 0.3.55 or newer first."
                )
            sites.append(site)

        active_runs = list(
            self.db.scalars(
                select(MaintenanceRun).where(
                    MaintenanceRun.kind.in_((self.PLUGIN_UPDATE_KIND, self.PLUGIN_INSTALLATION_KIND)),
                    MaintenanceRun.status == MaintenanceRunStatus.running.value,
                    MaintenanceRun.site_id.in_(requested_site_ids),
                )
            )
        )
        if active_runs:
            raise ValueError("An update or plugin installation is already queued or running for one of the selected websites.")

        batch_id = uuid4().hex
        now = datetime.now(UTC)
        runs: list[MaintenanceRun] = []
        for position, site in enumerate(sites, start=1):
            run = MaintenanceRun(
                site=site,
                kind=self.PLUGIN_INSTALLATION_KIND,
                status=MaintenanceRunStatus.running.value,
                requested_by=actor,
                started_at=now,
                plugin_installation_package_id=package.id,
                result_json={
                    "batch_id": batch_id,
                    "batch_position": position,
                    "batch_size": len(sites),
                    "package_source": package.source,
                    "plugin_name": package.plugin_name,
                    "plugin_file": package.plugin_file,
                    "target_version": package.plugin_version,
                    "package_sha256": package.sha256,
                    "activate": activate,
                    "replace_existing": replace_existing,
                    "stage": "queued",
                    "stage_message": "Queued after package and target-site preflight checks.",
                },
            )
            run.steps.extend(
                (
                    MaintenanceRunStep(
                        step_key="preflight",
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting to check the current plugin state on this website.",
                        result_json={},
                    ),
                    MaintenanceRunStep(
                        step_key="install-plugin",
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting for the package and target-site checks to pass.",
                        result_json={},
                    ),
                    MaintenanceRunStep(
                        step_key="postflight-health",
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting for the plugin installation to complete.",
                        result_json={},
                    ),
                )
            )
            self.db.add(run)
            runs.append(run)

        self.db.flush()
        for run in runs:
            write_audit_log(
                self.db,
                site=run.site,
                actor=actor,
                source="hub-web",
                action="start-plugin-installation-run",
                result="queued",
                detail=(
                    f"Queued plugin installation run {run.id} in batch {batch_id[:12]} for "
                    f"{package.plugin_name} {package.plugin_version}; activate={activate}, replace_existing={replace_existing}."
                ),
                request_id=batch_id,
            )
        self.db.commit()
        return PluginUpdateBatchOutcome(
            batch_id=batch_id,
            run_count=len(runs),
            message=(
                f"Queued {len(runs)} plugin installation{'s' if len(runs) != 1 else ''} for {package.plugin_name} {package.plugin_version}. "
                "Each target is prechecked, then verified with a health check afterwards."
            ),
        )

    def start_site_updates(
        self,
        *,
        site_id: int,
        selected_keys: list[str],
        actor: str,
    ) -> PluginUpdateBatchOutcome:
        """Start updates selected from one customer site's detail page only."""
        return self.start_direct_updates(
            selected_keys=selected_keys,
            actor=actor,
            expected_site_id=site_id,
        )

    def list_plugin_update_batch(self, batch_id: str) -> list[MaintenanceRun]:
        if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
            return []
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(MaintenanceRun.kind == self.PLUGIN_UPDATE_KIND)
            .order_by(MaintenanceRun.id.asc())
        )
        return [
            run
            for run in self.db.scalars(statement)
            if isinstance(run.result_json, dict) and run.result_json.get("batch_id") == batch_id
        ]

    def list_plugin_installation_batch(self, batch_id: str) -> list[MaintenanceRun]:
        if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
            return []
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(MaintenanceRun.kind == self.PLUGIN_INSTALLATION_KIND)
            .order_by(MaintenanceRun.id.asc())
        )
        return [
            run
            for run in self.db.scalars(statement)
            if isinstance(run.result_json, dict) and run.result_json.get("batch_id") == batch_id
        ]

    @staticmethod
    def direct_update_batch_cancellation_requested(runs: list[MaintenanceRun]) -> bool:
        return any(isinstance((run.result_json or {}).get("cancellation"), dict) for run in runs)

    @staticmethod
    def direct_update_batch_can_be_cancelled(runs: list[MaintenanceRun]) -> bool:
        return any(run.status == MaintenanceRunStatus.running.value for run in runs)

    def cancel_direct_update_batch(
        self,
        *,
        batch_id: str,
        actor: str,
    ) -> DirectUpdateBatchCancellationOutcome:
        """Stop queued direct updates while allowing an active WordPress request to finish."""
        batch_runs = self._direct_update_batch_runs(batch_id)
        if not batch_runs:
            raise ValueError("The direct update batch no longer exists.")

        now = datetime.now(UTC)
        cancellation = {"requested_at": now.isoformat(), "requested_by": actor}
        cancelled_queued_runs = 0
        processing_runs = 0
        cancellation_requested = False

        # Lock each run before changing it so a queued job cannot be claimed by a worker at the same time.
        for batch_run in batch_runs:
            statement = (
                select(MaintenanceRun)
                .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
                .where(MaintenanceRun.id == batch_run.id)
                .with_for_update()
            )
            run = self.db.scalar(statement)
            if run is None or self._plugin_update_batch_id(run) != batch_id:
                continue
            if run.status != MaintenanceRunStatus.running.value:
                continue

            result = dict(run.result_json or {})
            stage = result.get("stage", "queued")
            existing_cancellation = result.get("cancellation")
            if isinstance(existing_cancellation, dict):
                cancellation = existing_cancellation
            else:
                cancellation_requested = True

            if stage == "queued":
                message = "Cancelled before this update started."
                run.status = MaintenanceRunStatus.skipped.value
                run.completed_at = now
                run.last_checked_at = now
                run.error_message = None
                run.result_json = {
                    **result,
                    "stage": "cancelled",
                    "stage_message": message,
                    "cancellation": cancellation,
                }
                for step in run.steps:
                    if step.status in {MaintenanceRunStepStatus.waiting.value, MaintenanceRunStepStatus.running.value}:
                        step.status = MaintenanceRunStepStatus.skipped.value
                        step.completed_at = now
                        step.detail = message
                write_audit_log(
                    self.db,
                    site=run.site,
                    actor=actor,
                    source="hub-web",
                    action="cancel-direct-update-run",
                    result="cancelled",
                    detail=f"Cancelled queued direct update run {run.id} in batch {batch_id[:12]}.",
                    request_id=batch_id,
                )
                cancelled_queued_runs += 1
                continue

            # The worker owns an in-progress request. Record the cancellation, but let that request finish safely.
            run.result_json = {**result, "cancellation": cancellation}
            processing_runs += 1

        self.db.flush()
        return DirectUpdateBatchCancellationOutcome(
            batch_id=batch_id,
            cancelled_queued_runs=cancelled_queued_runs,
            processing_runs=processing_runs,
            cancellation_requested=cancellation_requested or processing_runs > 0,
        )

    def poll_active_plugin_updates(self, *, limit: int = 25) -> dict[str, int]:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(
                MaintenanceRun.kind == self.PLUGIN_UPDATE_KIND,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.asc(), MaintenanceRun.id.asc())
            .limit(limit)
        )
        runs = list(self.db.scalars(statement))
        summary = {"checked": 0, "succeeded": 0, "failed": 0, "waiting": 0, "skipped": 0}
        for run in runs:
            if run.status != MaintenanceRunStatus.running.value:
                continue
            summary["checked"] += 1
            outcome = self._poll_plugin_update(run)
            summary[outcome] += 1
            if outcome == "failed":
                batch_id = self._plugin_update_batch_id(run)
                if batch_id:
                    summary["skipped"] += self.stop_direct_update_batches_after_failure_streak({batch_id})
        return summary

    def direct_update_batch_ids_for_run_ids(self, run_ids: list[int]) -> set[str]:
        if not run_ids:
            return set()
        statement = select(MaintenanceRun).where(
            MaintenanceRun.id.in_(run_ids),
            MaintenanceRun.kind == self.PLUGIN_UPDATE_KIND,
        )
        return {
            batch_id
            for run in self.db.scalars(statement)
            if (batch_id := self._plugin_update_batch_id(run)) is not None
        }

    def stop_direct_update_batches_after_failure_streak(self, batch_ids: set[str]) -> int:
        """Stop only batches whose completed prefix contains five failed updates in a row."""
        skipped = 0
        for batch_id in batch_ids:
            runs = self._direct_update_batch_runs(batch_id)
            if not self._has_direct_update_failure_streak(runs):
                continue
            skipped += self._skip_queued_maintenance_runs(
                batch_id,
                kind=self.PLUGIN_UPDATE_KIND,
                message=(
                    f"Stopped after {self.DIRECT_UPDATE_FAILURE_STREAK_LIMIT} consecutive direct update failures. "
                    "Already running updates will finish."
                ),
            )
        return skipped

    def next_parallel_direct_update_run_ids(self, *, limit: int) -> list[int]:
        """Return one queued direct maintenance task per site, with stale runs recoverable."""
        if limit < 1:
            return []

        stale_before = datetime.now(UTC) - timedelta(minutes=10)
        statement = (
            select(MaintenanceRun)
            .where(
                MaintenanceRun.kind.in_((self.PLUGIN_UPDATE_KIND, self.PLUGIN_INSTALLATION_KIND)),
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.asc(), MaintenanceRun.id.asc())
        )
        selected_ids: list[int] = []
        selected_site_ids: set[int] = set()
        for run in self.db.scalars(statement):
            details = run.result_json if isinstance(run.result_json, dict) else {}
            stage = details.get("stage", "queued")
            stale_processing = (
                stage == "processing"
                and (run.last_checked_at is None or self._as_utc(run.last_checked_at) <= stale_before)
            )
            if stage != "queued" and not stale_processing:
                continue
            if run.site_id in selected_site_ids:
                continue
            selected_ids.append(run.id)
            selected_site_ids.add(run.site_id)
            if len(selected_ids) >= limit:
                break
        return selected_ids

    def poll_direct_update_run(self, run_id: int) -> str:
        """Backward-compatible entry point for the shared direct-maintenance worker."""
        return self.poll_direct_maintenance_run(run_id)

    def poll_direct_maintenance_run(self, run_id: int) -> str:
        """Run one queued update or plugin installation in its own database session."""
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(MaintenanceRun.id == run_id)
            .with_for_update()
        )
        run = self.db.scalar(statement)
        if run is None or run.kind not in {self.PLUGIN_UPDATE_KIND, self.PLUGIN_INSTALLATION_KIND}:
            return "skipped"
        if run.status != MaintenanceRunStatus.running.value:
            return "skipped"

        run.result_json = {
            **(run.result_json or {}),
            "stage": "processing",
            "stage_message": "Preparing this maintenance task in a dedicated worker.",
        }
        run.last_checked_at = datetime.now(UTC)
        self.db.commit()
        outcome = self._poll_plugin_update(run) if run.kind == self.PLUGIN_UPDATE_KIND else self._poll_plugin_installation(run)
        if outcome == "failed" and run.kind == self.PLUGIN_INSTALLATION_KIND:
            batch_id = self._plugin_update_batch_id(run)
            if batch_id:
                self._skip_queued_maintenance_runs(
                    batch_id,
                    kind=run.kind,
                    message=(
                        f"Skipped because maintenance run {run.id} failed. "
                        "The batch stops at the first error."
                    ),
                )
        return outcome

    def poll_complete_site_update_run(self, run_id: int) -> str:
        """Run one complete website workflow while preserving every component outcome."""
        run = self.db.scalar(
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(
                MaintenanceRun.id == run_id,
                MaintenanceRun.kind == self.COMPLETE_SITE_UPDATE_KIND,
            )
            .with_for_update()
        )
        if run is None or run.status != MaintenanceRunStatus.running.value:
            return "skipped"

        result = dict(run.result_json or {})
        result["stage"] = "processing"
        result["stage_message"] = "Starting the complete website update workflow."
        run.result_json = result
        run.last_checked_at = datetime.now(UTC)
        self._start_complete_site_update_step(
            run,
            "workflow",
            "Running fresh WordPress, theme, and plugin update waves.",
        )
        return self._poll_complete_site_update(run)

    def _poll_complete_site_update(self, run: MaintenanceRun) -> str:
        site = self.repository.get_site(run.site_id)
        if site is None or site.status != SiteStatus.verified.value:
            return self._finish_complete_site_update(
                run,
                status=MaintenanceRunStatus.failed.value,
                stage="failed",
                message="The site is no longer verified for a complete update workflow.",
            )

        state = dict(run.result_json or {})
        start_wave = max(1, int(state.get("wave", 0) or 0))
        failure_streak = 0
        for wave in range(start_wave, self.COMPLETE_SITE_UPDATE_MAX_WAVES + 1):
            if self._complete_site_update_cancellation_requested(run):
                return self._finish_complete_site_update(
                    run,
                    status=MaintenanceRunStatus.skipped.value,
                    stage="cancelled",
                    message="The complete update workflow was cancelled. Already completed updates remain documented.",
                )

            self._update_complete_site_update_state(
                run,
                workflow_phase="wordpress",
                wave=wave,
                stage="processing",
                stage_message=f"Starting update wave {wave} of {self.COMPLETE_SITE_UPDATE_MAX_WAVES}.",
            )
            for phase in ("wordpress", "theme", "plugin"):
                if self._complete_site_update_cancellation_requested(run):
                    return self._finish_complete_site_update(
                        run,
                        status=MaintenanceRunStatus.skipped.value,
                        stage="cancelled",
                        message="The complete update workflow was cancelled. Already completed updates remain documented.",
                    )

                entries, refresh_error = self._fresh_complete_site_update_entries(run, phase=phase, wave=wave)
                if refresh_error:
                    return self._finish_complete_site_update(
                        run,
                        status=MaintenanceRunStatus.failed.value,
                        stage="refresh-failed",
                        message=refresh_error,
                    )

                offered_entries = [entry for entry in entries if entry.update_available]
                executable_entries, blocked_entries = self._complete_site_update_entries_by_readiness(offered_entries)
                self._update_complete_site_update_state(
                    run,
                    workflow_phase=phase,
                    wave=wave,
                    stage="processing",
                    stage_message=(
                        f"{self._complete_site_update_phase_label(phase)}: "
                        f"{len(executable_entries)} executable update"
                        f"{'s' if len(executable_entries) != 1 else ''} found."
                    ),
                )
                if blocked_entries:
                    self._append_complete_site_update_event(
                        run,
                        phase=phase,
                        wave=wave,
                        status="blocked",
                        detail=(
                            f"{len(blocked_entries)} offered {self._complete_site_update_phase_label(phase).lower()} "
                            "update(s) are not currently executable."
                        ),
                        updates=[self._complete_site_update_entry_summary(entry) for entry in blocked_entries],
                    )

                for position, entry in enumerate(executable_entries, start=1):
                    if self._complete_site_update_cancellation_requested(run):
                        return self._finish_complete_site_update(
                            run,
                            status=MaintenanceRunStatus.skipped.value,
                            stage="cancelled",
                            message="The complete update workflow was cancelled. Already completed updates remain documented.",
                        )

                    self._update_complete_site_update_state(
                        run,
                        workflow_phase=phase,
                        wave=wave,
                        stage="processing",
                        stage_message=(
                            f"{self._complete_site_update_phase_label(phase)}: updating {entry.name} "
                            f"({position}/{len(executable_entries)})."
                        ),
                    )
                    self._append_complete_site_update_event(
                        run,
                        phase=phase,
                        wave=wave,
                        status="processing",
                        detail=f"Updating {entry.name} from {entry.current_version} to {entry.target_version}.",
                        update=self._complete_site_update_entry_summary(entry),
                    )
                    child_run = self._create_complete_site_update_child_run(run, entry, phase=phase, wave=wave)
                    outcome = self._poll_plugin_update(child_run)
                    child_result = dict(child_run.result_json or {})
                    child_message = child_run.error_message or child_result.get("stage_message") or "Completed."
                    self._record_complete_site_update_component_outcome(
                        run,
                        phase=phase,
                        wave=wave,
                        entry=entry,
                        outcome=outcome,
                        detail=str(child_message),
                        child_run_id=child_run.id,
                    )

                    if outcome == "failed":
                        health_failure = self._post_update_health_failure_kind(child_result.get("post_update_health"))
                        if health_failure is not None:
                            return self._finish_complete_site_update(
                                run,
                                status=MaintenanceRunStatus.failed.value,
                                stage="post-update-health-failed",
                                message=(
                                    f"Stopped after {entry.name} because its post-update "
                                    f"{self._post_update_health_failure_label(health_failure)} check did not pass. "
                                    "No further updates were started for this website."
                                ),
                            )
                        failure_streak += 1
                        if phase == "wordpress":
                            return self._finish_complete_site_update(
                                run,
                                status=MaintenanceRunStatus.failed.value,
                                stage="core-update-failed",
                                message=(
                                    "The WordPress core update failed, so the complete workflow stopped before "
                                    "theme and plugin updates."
                                ),
                            )
                        if failure_streak >= self.DIRECT_UPDATE_FAILURE_STREAK_LIMIT:
                            return self._finish_complete_site_update(
                                run,
                                status=MaintenanceRunStatus.failed.value,
                                stage="failure-limit-reached",
                                message=(
                                    f"Stopped after {self.DIRECT_UPDATE_FAILURE_STREAK_LIMIT} consecutive "
                                    "theme or plugin update failures."
                                ),
                            )
                    else:
                        failure_streak = 0

            final_entries, refresh_error = self._fresh_complete_site_update_entries(run, phase="verification", wave=wave)
            if refresh_error:
                return self._finish_complete_site_update(
                    run,
                    status=MaintenanceRunStatus.failed.value,
                    stage="refresh-failed",
                    message=refresh_error,
                )
            remaining_entries = [entry for entry in final_entries if entry.update_available]
            executable_remaining, blocked_remaining = self._complete_site_update_entries_by_readiness(remaining_entries)
            if not remaining_entries:
                return self._finish_complete_site_update(
                    run,
                    status=MaintenanceRunStatus.succeeded.value,
                    stage="completed",
                    message="No further WordPress, theme, or plugin updates are currently offered by this website.",
                )
            if not executable_remaining:
                self._append_complete_site_update_event(
                    run,
                    phase="verification",
                    wave=wave,
                    status="blocked",
                    detail="Fresh verification still reports offered updates that are not executable.",
                    updates=[self._complete_site_update_entry_summary(entry) for entry in blocked_remaining],
                )
                return self._finish_complete_site_update(
                    run,
                    status=MaintenanceRunStatus.failed.value,
                    stage="updates-blocked",
                    message="Fresh verification found updates that are currently not executable. Review the recorded blockers.",
                )
            if wave == self.COMPLETE_SITE_UPDATE_MAX_WAVES:
                return self._finish_complete_site_update(
                    run,
                    status=MaintenanceRunStatus.failed.value,
                    stage="wave-limit-reached",
                    message=(
                        f"Fresh verification still found updates after {self.COMPLETE_SITE_UPDATE_MAX_WAVES} waves. "
                        "The remaining offers are documented for review."
                    ),
                )
        return self._finish_complete_site_update(
            run,
            status=MaintenanceRunStatus.failed.value,
            stage="wave-limit-reached",
            message="The complete update workflow ended before reaching a stable update state.",
        )

    def _fresh_complete_site_update_entries(
        self,
        run: MaintenanceRun,
        *,
        phase: str,
        wave: int,
    ) -> tuple[list[UpdateWorkbenchEntry], str | None]:
        label = "final verification" if phase == "verification" else self._complete_site_update_phase_label(phase)
        step_key = f"wave-{wave}-{phase}-refresh"
        self._start_complete_site_update_step(run, step_key, f"Refreshing available updates before {label}.")
        try:
            SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(run.site_id)
        except SiteMcpProxyError as exc:
            self._complete_complete_site_update_step(run, step_key, "failed", f"Fresh update check failed: {exc.message}")
            return [], f"Fresh update check before {label} failed: {exc.message}"

        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        entries = [
            entry
            for entry in inventory.build_update_workbench(inventory.list_items(limit=1000))
            if entry.site.id == run.site_id
        ]
        if phase != "verification":
            entries = [entry for entry in entries if entry.kind == phase]
        available_count = sum(entry.update_available for entry in entries)
        self._complete_complete_site_update_step(
            run,
            step_key,
            "succeeded",
            f"Fresh update check found {available_count} available {label.lower()} update(s).",
            result={"available_updates": available_count},
        )
        self._append_complete_site_update_event(
            run,
            phase=phase,
            wave=wave,
            status="refreshed",
            detail=f"Fresh update check found {available_count} available {label.lower()} update(s).",
        )
        return entries, None

    def _create_complete_site_update_child_run(
        self,
        workflow_run: MaintenanceRun,
        entry: UpdateWorkbenchEntry,
        *,
        phase: str,
        wave: int,
    ) -> MaintenanceRun:
        now = datetime.now(UTC)
        update_step_key = self._update_step_key(entry.kind)
        run = MaintenanceRun(
            site=workflow_run.site,
            kind=self.PLUGIN_UPDATE_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by=workflow_run.requested_by,
            started_at=now,
            result_json={
                "workflow_run_id": workflow_run.id,
                "workflow_phase": phase,
                "workflow_wave": wave,
                "update_kind": entry.kind,
                "update_identifier": entry.identifier,
                "update_name": entry.name,
                "current_version": entry.current_version,
                "target_version": entry.target_version,
                "expected_active": entry.is_active,
                # The workflow owns this run synchronously; the direct-update worker must not pick it up.
                "stage": "workflow-processing",
                "stage_message": "Running as part of the complete website update workflow.",
            },
        )
        run.steps.extend(
            (
                MaintenanceRunStep(
                    step_key="preflight",
                    status=MaintenanceRunStepStatus.waiting.value,
                    started_at=now,
                    detail="Waiting for the final on-site Bridge update check.",
                    result_json={},
                ),
                MaintenanceRunStep(
                    step_key=update_step_key,
                    status=MaintenanceRunStepStatus.waiting.value,
                    started_at=now,
                    detail="Waiting for the preflight to pass.",
                    result_json={},
                ),
                MaintenanceRunStep(
                    step_key="postflight-health",
                    status=MaintenanceRunStepStatus.waiting.value,
                    started_at=now,
                    detail="Waiting for the selected update to complete.",
                    result_json={},
                ),
            )
        )
        self.db.add(run)
        self.db.flush()
        self.db.commit()
        return run

    def _complete_site_update_entries_by_readiness(
        self,
        entries: list[UpdateWorkbenchEntry],
    ) -> tuple[list[UpdateWorkbenchEntry], list[UpdateWorkbenchEntry]]:
        has_stored_crocoblock_license = self._has_stored_crocoblock_license()
        executable: list[UpdateWorkbenchEntry] = []
        blocked: list[UpdateWorkbenchEntry] = []
        for entry in entries:
            scope_error = self._direct_plugin_update_scope_error(
                entry,
                allow_stored_crocoblock_license=True,
                has_stored_crocoblock_license=has_stored_crocoblock_license,
            )
            if entry.direct_update_selectable and scope_error is None:
                executable.append(entry)
            else:
                blocked.append(entry)
        return executable, blocked

    def _record_complete_site_update_component_outcome(
        self,
        run: MaintenanceRun,
        *,
        phase: str,
        wave: int,
        entry: UpdateWorkbenchEntry,
        outcome: str,
        detail: str,
        child_run_id: int,
    ) -> None:
        result = dict(run.result_json or {})
        counter_key = {
            "succeeded": "successful_updates",
            "failed": "failed_updates",
            "skipped": "skipped_updates",
        }.get(outcome, "failed_updates")
        result[counter_key] = int(result.get(counter_key, 0) or 0) + 1
        run.result_json = result
        self._append_complete_site_update_event(
            run,
            phase=phase,
            wave=wave,
            status=outcome,
            detail=detail,
            update={**self._complete_site_update_entry_summary(entry), "run_id": child_run_id},
        )

    @staticmethod
    def _complete_site_update_entry_summary(entry: UpdateWorkbenchEntry) -> dict[str, str]:
        return {
            "kind": entry.kind,
            "name": entry.name,
            "identifier": entry.identifier,
            "current_version": entry.current_version,
            "target_version": entry.target_version,
        }

    @staticmethod
    def _complete_site_update_phase_label(phase: str) -> str:
        return {
            "wordpress": "WordPress",
            "theme": "Themes",
            "plugin": "Plugins",
            "verification": "Final verification",
        }.get(phase, "Workflow")

    def _complete_site_update_cancellation_requested(self, run: MaintenanceRun) -> bool:
        # A cancellation is written by a separate request/session while this worker is running.
        self.db.refresh(run, attribute_names=["result_json"])
        return isinstance((run.result_json or {}).get("cancellation"), dict)

    def _update_complete_site_update_state(self, run: MaintenanceRun, **updates: Any) -> None:
        result = dict(run.result_json or {})
        result.update(updates)
        run.result_json = result
        run.last_checked_at = datetime.now(UTC)
        self.db.commit()

    def _append_complete_site_update_event(
        self,
        run: MaintenanceRun,
        *,
        phase: str,
        wave: int,
        status: str,
        detail: str,
        update: dict[str, Any] | None = None,
        updates: list[dict[str, Any]] | None = None,
    ) -> None:
        result = dict(run.result_json or {})
        events = list(result.get("events", []))
        event: dict[str, Any] = {
            "phase": phase,
            "wave": wave,
            "status": status,
            "detail": detail,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        if update is not None:
            event["update"] = update
        if updates is not None:
            event["updates"] = updates
        events.append(event)
        result["events"] = events[-150:]
        run.result_json = result
        run.last_checked_at = datetime.now(UTC)
        self.db.commit()

    def _start_complete_site_update_step(self, run: MaintenanceRun, step_key: str, detail: str) -> None:
        now = datetime.now(UTC)
        step = self._find_step(run, step_key)
        if step is None:
            step = MaintenanceRunStep(
                step_key=step_key,
                status=MaintenanceRunStepStatus.running.value,
                started_at=now,
                detail=detail,
                result_json={},
            )
            run.steps.append(step)
        else:
            step.status = MaintenanceRunStepStatus.running.value
            step.started_at = now
            step.completed_at = None
            step.detail = detail
        run.last_checked_at = now
        self.db.commit()

    def _complete_complete_site_update_step(
        self,
        run: MaintenanceRun,
        step_key: str,
        status: str,
        detail: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> None:
        step = self._find_step(run, step_key)
        if step is None:
            return
        step.status = status
        step.completed_at = datetime.now(UTC)
        step.detail = detail
        step.result_json = dict(result or {})
        run.last_checked_at = step.completed_at
        self.db.commit()

    def _finish_complete_site_update(
        self,
        run: MaintenanceRun,
        *,
        status: str,
        stage: str,
        message: str,
    ) -> str:
        completed_at = datetime.now(UTC)
        result = dict(run.result_json or {})
        terminal_phase = {
            MaintenanceRunStatus.succeeded.value: "completed",
            MaintenanceRunStatus.failed.value: "failed",
            MaintenanceRunStatus.skipped.value: "cancelled",
        }.get(status, stage)
        result.update(
            {
                "stage": stage,
                "stage_message": message,
                # Do not leave the last active phase visible after the workflow has ended.
                "workflow_phase": terminal_phase,
            }
        )
        run.status = status
        run.completed_at = completed_at
        run.last_checked_at = completed_at
        run.error_message = message if status == MaintenanceRunStatus.failed.value else None
        run.result_json = result
        workflow_step = self._find_step(run, "workflow")
        if workflow_step is not None:
            workflow_step.status = (
                MaintenanceRunStepStatus.succeeded.value
                if status == MaintenanceRunStatus.succeeded.value
                else MaintenanceRunStepStatus.skipped.value
                if status == MaintenanceRunStatus.skipped.value
                else MaintenanceRunStepStatus.failed.value
            )
            workflow_step.completed_at = completed_at
            workflow_step.detail = message
        for step in run.steps:
            if step is workflow_step:
                continue
            if step.status in {MaintenanceRunStepStatus.waiting.value, MaintenanceRunStepStatus.running.value}:
                step.status = MaintenanceRunStepStatus.skipped.value
                step.completed_at = completed_at
                step.detail = "Not run because the complete workflow finished."
        write_audit_log(
            self.db,
            site=run.site,
            actor="kosmos-hub",
            source="hub-worker",
            action="complete-site-update-run",
            result=status,
            detail=f"Complete site update run {run.id}: {message}",
            request_id=str(run.id),
        )
        self.db.commit()
        return "succeeded" if status == MaintenanceRunStatus.succeeded.value else "skipped" if status == MaintenanceRunStatus.skipped.value else "failed"

    def fail_direct_update_worker_run(self, run_id: int) -> None:
        """Persist an unexpected worker failure instead of leaving a run stuck."""
        self.fail_direct_maintenance_worker_run(run_id)

    def fail_complete_site_update_worker_run(self, run_id: int) -> None:
        run = self.get_complete_site_update_run(run_id)
        if run is None or run.status != MaintenanceRunStatus.running.value:
            return
        self._finish_complete_site_update(
            run,
            status=MaintenanceRunStatus.failed.value,
            stage="worker-failed",
            message=(
                "The Hub complete-update worker stopped unexpectedly. "
                "Already completed component updates remain documented."
            ),
        )

    def fail_direct_maintenance_worker_run(self, run_id: int) -> None:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(MaintenanceRun.id == run_id)
        )
        run = self.db.scalar(statement)
        if run is None or run.kind not in {self.PLUGIN_UPDATE_KIND, self.PLUGIN_INSTALLATION_KIND}:
            return
        if run.status != MaintenanceRunStatus.running.value:
            return
        self._fail_plugin_update_run(
            run,
            "The Hub maintenance worker stopped unexpectedly. The website was not marked as successfully changed.",
        )

    def _poll_plugin_update(self, run: MaintenanceRun) -> str:
        details = self._direct_update_details(run)
        site = self.repository.get_site(run.site_id)
        if site is None or site.status != SiteStatus.verified.value:
            self._fail_plugin_update_run(run, "The site is no longer verified for direct updates.")
            return "failed"
        if details is None:
            self._fail_plugin_update_run(run, "The direct update run has an invalid update scope.")
            return "failed"

        preflight_step = self._find_step(run, "preflight")
        bridge_enforces_preflight = self._bridge_enforces_final_update_preflight(site)
        if bridge_enforces_preflight:
            self._start_plugin_update_step(
                run,
                preflight_step,
                "The Bridge will validate the installed version and update package immediately before installation.",
            )
            crocoblock_error = self._activate_crocoblock_license_if_required(
                run,
                details,
                preflight_step,
                refresh_authorized_offer=False,
            )
            if crocoblock_error:
                self._fail_plugin_update_run(run, crocoblock_error)
                return "failed"
            self._complete_plugin_update_step(
                run,
                preflight_step,
                "The Bridge will enforce the final installed-version, activation-state, and package checks during the update.",
            )
        else:
            # Older Bridges do not return enough mismatch evidence for safe local reconciliation.
            self._start_plugin_update_step(run, preflight_step, "Refreshing available-update evidence.")
            try:
                SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(run.site_id)
            except SiteMcpProxyError as exc:
                self._fail_plugin_update_run(run, f"Direct update preflight failed: {exc.message}")
                return "failed"

            crocoblock_error = self._activate_crocoblock_license_if_required(run, details, preflight_step)
            if crocoblock_error:
                self._fail_plugin_update_run(run, crocoblock_error)
                return "failed"

            preflight_error, preflight_note = self._direct_plugin_update_preflight(run, details)
            if preflight_error:
                reconciled_outcome = self._reconcile_direct_plugin_preflight_mismatch(
                    run,
                    details,
                    preflight_step,
                    preflight_error,
                )
                if reconciled_outcome is not None:
                    return reconciled_outcome
                self._fail_plugin_update_run(run, preflight_error)
                return "failed"
            self._complete_plugin_update_step(
                run,
                preflight_step,
                f"Fresh selected update checks passed.{preflight_note}",
            )

        update_step = self._find_step(run, self._update_step_key(details["update_kind"]))
        self._start_plugin_update_step(
            run,
            update_step,
            f"Updating {details['update_name']} from {details['current_version']} to {details['target_version']}.",
        )
        if bridge_enforces_preflight:
            update_outcome, payload = self._execute_direct_update_with_final_bridge_preflight(
                run,
                details,
                preflight_step,
                update_step,
            )
            if update_outcome != "updated":
                return update_outcome
        else:
            try:
                payload = self._execute_direct_update(run.site_id, details)
            except SiteMcpProxyError as exc:
                reconciled_result, reconciliation_detail = self._reconcile_plugin_after_failed_update_request(
                    run,
                    details,
                    update_step,
                    exc,
                )
                if reconciled_result is None:
                    message = f"{details['update_name']} update request failed: {exc.message}"
                    if reconciliation_detail:
                        message = f"{message} Post-update reconciliation did not succeed: {reconciliation_detail}"
                    self._fail_plugin_update_run(run, message)
                    return "failed"
                payload = reconciled_result

        result = self._result_from_payload(payload)
        result_error = self._direct_update_result_error(details, result)
        if result_error:
            self._fail_plugin_update_run(run, result_error)
            return "failed"

        self._complete_plugin_update_step(
            run,
            update_step,
            self._direct_update_verification_detail(details, result),
            result,
        )

        health_step = self._find_step(run, "postflight-health")
        self._wait_for_post_update_framework_stabilization(run, details, health_step)
        self._start_plugin_update_step(
            run,
            health_step,
            "Checking the public homepage, WordPress REST API, and admin AJAX endpoint.",
        )
        health_error, health_detail, health_result = self._run_direct_update_postflight_health(
            run,
            health_step,
        )
        if health_error:
            run.result_json = {
                **(run.result_json or {}),
                "post_update_health": health_result,
            }
            self._fail_plugin_update_run(
                run,
                f"{details['update_name']} was updated and verified, but {health_error}. No automatic rollback was performed.",
                health_step=health_step,
            )
            return "failed"

        return self._complete_confirmed_direct_update_run(
            run,
            details,
            result,
            health_step,
            health_detail,
            health_result,
            recovery_note=self._direct_update_reconciliation_note(result),
        )

    def _wait_for_post_update_framework_stabilization(
        self,
        run: MaintenanceRun,
        details: dict[str, Any],
        health_step: MaintenanceRunStep | None,
    ) -> None:
        """Give active WordPress frameworks time to finish their in-place upgrade cleanup."""
        if not self._requires_post_update_framework_stabilization(details):
            return

        seconds = self.POST_UPDATE_FRAMEWORK_STABILIZATION_SECONDS
        self._start_plugin_update_step(
            run,
            health_step,
            (
                f"Allowing {details['update_name']} {seconds} seconds to finish WordPress "
                "update cleanup before the admin health check."
            ),
        )
        time.sleep(seconds)

    @classmethod
    def _requires_post_update_framework_stabilization(cls, details: dict[str, Any]) -> bool:
        if details["update_kind"] == "wordpress":
            return True
        return (
            details["update_kind"] == "plugin"
            and details.get("expected_active") is True
            and details["update_identifier"] in cls.POST_UPDATE_FRAMEWORK_PLUGIN_IDENTIFIERS
        )

    def _record_confirmed_direct_update(
        self,
        run: MaintenanceRun,
        details: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Persist the Bridge-confirmed change without issuing full post-update scans."""
        installed_version = str(result.get("installed_version", "")).strip()
        active = result.get("active") if isinstance(result.get("active"), bool) else None
        SiteInventoryService(db=self.db, cipher=self.cipher).record_confirmed_direct_update(
            site_id=run.site_id,
            update_kind=details["update_kind"],
            identifier=details["update_identifier"],
            installed_version=installed_version,
            active=active,
        )
        SiteUpdateService(db=self.db, cipher=self.cipher).record_confirmed_direct_update(
            site_id=run.site_id,
            update_kind=details["update_kind"],
            identifier=details["update_identifier"],
        )

    def _complete_confirmed_direct_update_run(
        self,
        run: MaintenanceRun,
        details: dict[str, Any],
        update_result: dict[str, Any],
        health_step: MaintenanceRunStep | None,
        health_detail: str,
        health_result: dict[str, Any],
        *,
        recovery_note: str = "",
    ) -> str:
        """Persist a Bridge-confirmed update after its non-blocking health checks pass."""
        self._complete_plugin_update_step(run, health_step, health_detail, health_result)
        self._record_confirmed_direct_update(run, details, update_result)

        completed_at = datetime.now(UTC)
        stage_message = f"{details['update_name']} was updated and verified. Stored inventory and update offers were reconciled locally."
        if recovery_note:
            stage_message = f"{stage_message} {recovery_note}"
        run.status = MaintenanceRunStatus.succeeded.value
        run.completed_at = completed_at
        run.last_checked_at = completed_at
        run.error_message = None
        run.result_json = {
            **(run.result_json or {}),
            "stage": "completed",
            "stage_message": stage_message,
            "installed_version": details["target_version"],
            "post_update_health": health_result,
        }
        write_audit_log(
            self.db,
            site=run.site,
            actor="kosmos-hub",
            source="hub-worker",
            action="complete-direct-update-run",
            result="succeeded",
            detail=(
                f"Direct update run {run.id} updated {details['update_name']} "
                f"{details['current_version']} -> {details['target_version']}. {health_detail} "
                f"Stored inventory and update offers were reconciled locally. {recovery_note}"
            ).strip(),
            request_id=self._plugin_update_batch_id(run),
        )
        self.db.commit()
        return "succeeded"

    def _poll_plugin_installation(self, run: MaintenanceRun) -> str:
        details = self._plugin_installation_details(run)
        site = self.repository.get_site(run.site_id)
        if site is None or site.status != SiteStatus.verified.value:
            self._fail_plugin_update_run(run, "The site is no longer verified for plugin installation.")
            return "failed"
        if details is None:
            self._fail_plugin_update_run(run, "The plugin installation run has invalid package metadata.")
            return "failed"
        if self.PLUGIN_INSTALLATION_ABILITY not in {capability.ability_name for capability in site.capabilities}:
            self._fail_plugin_update_run(run, "This site no longer reports the Kosmos Bridge plugin-installation ability.")
            return "failed"

        preflight_step = self._find_step(run, "preflight")
        self._start_plugin_update_step(run, preflight_step, "Reading the current installed plugin state from the website.")
        try:
            payload = self.proxy.execute_readonly_ability(
                run.site_id,
                self.LIST_INSTALLED_PLUGINS_ABILITY,
                None,
                timeout_seconds=45,
            )
        except SiteMcpProxyError as exc:
            self._fail_plugin_update_run(run, f"Plugin installation preflight failed: {exc.message}")
            return "failed"

        current_plugins = self._result_from_payload(payload).get("plugins", [])
        installed = next(
            (
                plugin
                for plugin in current_plugins
                if isinstance(plugin, dict) and plugin.get("plugin_file") == details["plugin_file"]
            ),
            None,
        )
        if installed is not None and not details["replace_existing"]:
            self._fail_plugin_update_run(
                run,
                f"{details['plugin_name']} is already installed on this website. Enable replacement to install this checked package over it.",
            )
            return "failed"
        previous_version = str(installed.get("version", "")) if isinstance(installed, dict) else ""
        self._complete_plugin_update_step(
            run,
            preflight_step,
            (
                f"Target-site preflight passed. "
                f"{'Replacing installed version ' + previous_version if previous_version else 'The plugin is not installed yet'}."
            ),
            {"previous_version": previous_version, "already_installed": installed is not None},
        )

        install_step = self._find_step(run, "install-plugin")
        self._start_plugin_update_step(
            run,
            install_step,
            f"Installing checked package {details['plugin_name']} {details['target_version']}.",
        )
        try:
            payload = self.proxy.execute_ability(
                run.site_id,
                self.PLUGIN_INSTALLATION_ABILITY,
                {
                    "package_id": details["package_id"],
                    "plugin_file": details["plugin_file"],
                    "expected_version": details["target_version"],
                    "package_sha256": details["package_sha256"],
                    "activate": details["activate"],
                    "replace_existing": details["replace_existing"],
                },
                timeout_seconds=300,
            )
        except SiteMcpProxyError as exc:
            self._fail_plugin_update_run(run, f"Plugin installation request failed: {exc.message}")
            return "failed"

        result = self._result_from_payload(payload)
        installation_error = self._plugin_installation_result_error(details, result)
        if installation_error:
            self._fail_plugin_update_run(run, installation_error)
            return "failed"
        self._complete_plugin_update_step(
            run,
            install_step,
            (
                f"Bridge verified {details['plugin_name']} {details['target_version']} "
                f"{'active' if result.get('active') else 'inactive'} after installation."
            ),
            result,
        )

        health_step = self._find_step(run, "postflight-health")
        self._start_plugin_update_step(run, health_step, "Checking the public homepage and WordPress REST API.")
        health_error, health_detail, health_result = self._run_direct_update_postflight_health(run, health_step)
        if health_error:
            self._fail_plugin_update_run(
                run,
                f"{details['plugin_name']} was installed and verified, but {health_error}. No automatic rollback was performed.",
                health_step=health_step,
            )
            return "failed"
        self._complete_plugin_update_step(run, health_step, health_detail, health_result)

        refresh_note = ""
        try:
            SiteInventoryService(db=self.db, cipher=self.cipher).refresh_site_state(run.site_id)
            SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(run.site_id)
        except SiteMcpProxyError as exc:
            refresh_note = f" The follow-up inventory scan failed: {exc.message}"

        completed_at = datetime.now(UTC)
        run.status = MaintenanceRunStatus.succeeded.value
        run.completed_at = completed_at
        run.last_checked_at = completed_at
        run.result_json = {
            **(run.result_json or {}),
            "stage": "completed",
            "stage_message": f"{details['plugin_name']} was installed and verified.{refresh_note}",
            "installed_version": details["target_version"],
            "active": result.get("active") is True,
        }
        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-hub",
            source="hub-worker",
            action="complete-plugin-installation-run",
            result="succeeded",
            detail=(
                f"Plugin installation run {run.id} installed {details['plugin_name']} "
                f"{details['target_version']}. {health_detail}{refresh_note}"
            ),
            request_id=self._plugin_update_batch_id(run),
        )
        self.db.commit()
        return "succeeded"

    def _run_direct_update_postflight_health(
        self,
        run: MaintenanceRun,
        health_step: MaintenanceRunStep | None,
    ) -> tuple[str | None, str, dict[str, Any]]:
        """Allow WordPress a short stabilization window after an in-place update."""
        last_error = "the Bridge did not return a verifiable health result"
        last_result: dict[str, Any] = {}

        for attempt in range(1, self.POST_UPDATE_HEALTH_MAX_ATTEMPTS + 1):
            try:
                health_payload = self.proxy.execute_ability(
                    run.site_id,
                    self.SITE_HEALTH_ABILITY,
                    None,
                    timeout_seconds=45,
                )
            except SiteMcpProxyError as exc:
                last_error = f"the Bridge health check could not run ({exc.message})"
            else:
                health_result = self._result_from_payload(health_payload)
                if isinstance(health_result, dict):
                    last_result = health_result
                health_error = self._plugin_update_health_error(health_result)
                if health_error is None:
                    return None, self._plugin_update_health_detail(health_result), last_result
                last_error = health_error

            if attempt == self.POST_UPDATE_HEALTH_MAX_ATTEMPTS:
                break

            self._start_plugin_update_step(
                run,
                health_step,
                (
                    f"Post-update health check attempt {attempt} did not pass ({last_error}). "
                    f"Retrying in {self.POST_UPDATE_HEALTH_RETRY_DELAY_SECONDS} seconds while WordPress finishes initialization."
                ),
            )
            time.sleep(self.POST_UPDATE_HEALTH_RETRY_DELAY_SECONDS)

        return (
            last_error,
            (
                f"Post-update health check did not pass after {self.POST_UPDATE_HEALTH_MAX_ATTEMPTS} attempts: "
                f"{last_error}."
            ),
            last_result,
        )

    @classmethod
    def _bridge_enforces_final_update_preflight(cls, site: Site) -> bool:
        """Only newer Bridges return the evidence needed to reconcile a changed offer safely."""
        return cls._installed_version_meets_target(
            str(site.bridge_version or ""),
            cls.FINAL_BRIDGE_PREFLIGHT_MIN_VERSION,
        )

    def _execute_direct_update_with_final_bridge_preflight(
        self,
        run: MaintenanceRun,
        details: dict[str, Any],
        preflight_step: MaintenanceRunStep | None,
        update_step: MaintenanceRunStep | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Let the Bridge enforce the final state, adopting only its structured fresh evidence."""
        retries = 0
        while True:
            try:
                return "updated", self._execute_direct_update(run.site_id, details)
            except SiteMcpProxyError as exc:
                resolution = self._bridge_update_preflight_resolution(details, exc)
                if resolution is None:
                    reconciled_result, reconciliation_detail = self._reconcile_plugin_after_failed_update_request(
                        run,
                        details,
                        update_step,
                        exc,
                    )
                    if reconciled_result is not None:
                        return "updated", reconciled_result
                    message = f"{details['update_name']} update request failed: {exc.message}"
                    if reconciliation_detail:
                        message = f"{message} Post-update reconciliation did not succeed: {reconciliation_detail}"
                    self._fail_plugin_update_run(run, message)
                    return "failed", None

                if resolution["action"] == "activate-crocoblock":
                    if (run.result_json or {}).get("crocoblock_license_activation_attempted") is True:
                        resolution = self._bridge_update_unavailable_resolution(details, exc)
                    else:
                        crocoblock_error = self._activate_crocoblock_license_if_required(
                            run,
                            details,
                            preflight_step,
                            force=True,
                            refresh_authorized_offer=False,
                        )
                        if crocoblock_error:
                            self._fail_plugin_update_run(run, crocoblock_error)
                            return "failed", None
                        retries += 1
                        self._start_plugin_update_step(
                            run,
                            update_step,
                            "Crocoblock license activation completed. Retrying the Bridge update with its fresh package check.",
                        )
                        continue

                if resolution["action"] == "retry" and retries < self.FINAL_BRIDGE_PREFLIGHT_RETRY_LIMIT:
                    retries += 1
                    self._adopt_bridge_update_preflight_values(run, details, resolution)
                    self._start_plugin_update_step(run, update_step, str(resolution["message"]))
                    continue

                if resolution["action"] == "retry":
                    resolution = self._bridge_update_unavailable_resolution(details, exc, repeated=True)

                self._finish_direct_plugin_preflight_resolution(
                    run,
                    preflight_step,
                    details,
                    resolution,
                    update_step=update_step,
                )
                return str(resolution["outcome"]), None

    def _reconcile_plugin_after_failed_update_request(
        self,
        run: MaintenanceRun,
        details: dict[str, Any],
        update_step: MaintenanceRunStep | None,
        update_error: SiteMcpProxyError,
    ) -> tuple[dict[str, Any] | None, str]:
        """Reconcile a plugin update error with the state WordPress actually reports."""
        if details["update_kind"] != "plugin":
            return None, ""

        self._start_plugin_update_step(
            run,
            update_step,
            (
                "The Bridge update request returned an error. Reading the installed plugin state "
                "before classifying the update result."
            ),
        )
        reconciliation = {
            "attempted": True,
            "original_error": update_error.message,
            "original_error_code": update_error.code,
            "original_error_status": update_error.status_code,
            "plugin_file": details["update_identifier"],
            "target_version": details["target_version"],
            "expected_active": details["expected_active"],
        }
        try:
            payload = self.proxy.execute_readonly_ability(
                run.site_id,
                self.LIST_INSTALLED_PLUGINS_ABILITY,
                None,
                timeout_seconds=45,
            )
        except SiteMcpProxyError as recovery_error:
            run.result_json = {
                **(run.result_json or {}),
                "post_update_reconciliation": {
                    **reconciliation,
                    "confirmed": False,
                    "error": recovery_error.message,
                },
            }
            self.db.commit()
            return None, recovery_error.message

        available_plugins = self._result_from_payload(payload).get("plugins", [])
        plugins = available_plugins if isinstance(available_plugins, list) else []
        installed = next(
            (
                plugin
                for plugin in plugins
                if isinstance(plugin, dict) and plugin.get("plugin_file") == details["update_identifier"]
            ),
            None,
        )
        installed_version = str(installed.get("version", "")).strip() if isinstance(installed, dict) else ""
        installed_active = installed.get("active") if isinstance(installed, dict) and isinstance(installed.get("active"), bool) else None
        if installed_version != details["target_version"]:
            run.result_json = {
                **(run.result_json or {}),
                "post_update_reconciliation": {
                    **reconciliation,
                    "confirmed": False,
                    "installed_version": installed_version,
                    "installed_active": installed_active,
                    "error": "WordPress did not confirm the planned target version.",
                },
            }
            self.db.commit()
            return None, "WordPress did not confirm the planned target version."

        if installed_active != details["expected_active"]:
            if details["expected_active"] is not True:
                run.result_json = {
                    **(run.result_json or {}),
                    "post_update_reconciliation": {
                        **reconciliation,
                        "confirmed": False,
                        "installed_version": installed_version,
                        "installed_active": installed_active,
                        "error": "WordPress did not preserve the approved inactive state.",
                    },
                }
                self.db.commit()
                return None, "WordPress did not preserve the approved inactive state."

            activation_recovery_attempted = True
            self._start_plugin_update_step(
                run,
                update_step,
                "WordPress confirmed the target version but not its active state. Restoring the approved active state.",
            )
            try:
                payload = self.proxy.execute_ability(
                    run.site_id,
                    self.PLUGIN_ACTIVATION_ABILITY,
                    {
                        "plugin_file": details["update_identifier"],
                        "expected_installed_version": details["target_version"],
                    },
                    timeout_seconds=60,
                )
            except SiteMcpProxyError as activation_error:
                run.result_json = {
                    **(run.result_json or {}),
                    "post_update_reconciliation": {
                        **reconciliation,
                        "confirmed": False,
                        "installed_version": installed_version,
                        "installed_active": installed_active,
                        "activation_recovery_attempted": True,
                        "error": activation_error.message,
                    },
                }
                self.db.commit()
                return None, activation_error.message

            activation_result = self._result_from_payload(payload)
            installed_active = activation_result.get("active") if isinstance(activation_result.get("active"), bool) else None
            if (
                activation_result.get("plugin_file") != details["update_identifier"]
                or activation_result.get("installed_version") != details["target_version"]
                or installed_active is not True
            ):
                run.result_json = {
                    **(run.result_json or {}),
                    "post_update_reconciliation": {
                        **reconciliation,
                        "confirmed": False,
                        "installed_version": installed_version,
                        "installed_active": installed_active,
                        "activation_recovery_attempted": True,
                        "error": "The Bridge did not confirm the planned version and active state.",
                        "result": activation_result,
                    },
                }
                self.db.commit()
                return None, "The Bridge did not confirm the planned version and active state."
        else:
            activation_recovery_attempted = False

        run.result_json = {
            **(run.result_json or {}),
            "post_update_reconciliation": {
                **reconciliation,
                "confirmed": True,
                "installed_version": details["target_version"],
                "installed_active": details["expected_active"],
                "activation_recovery_attempted": activation_recovery_attempted,
                "activated": activation_recovery_attempted and activation_result.get("activated") is True,
            },
        }
        self.db.commit()
        return {
            "updated": True,
            "plugin_file": details["update_identifier"],
            "previous_version": details["current_version"],
            "installed_version": details["target_version"],
            "active": details["expected_active"],
            "reconciled_after_update_error": True,
            "post_update_error": update_error.message,
        }, ""

    @classmethod
    def _bridge_update_preflight_resolution(
        cls,
        details: dict[str, Any],
        error: SiteMcpProxyError,
    ) -> dict[str, Any] | None:
        """Turn a final Bridge preflight conflict into a safe retry or a documented terminal result."""
        code = error.code.upper()
        evidence = error.details
        installed_version = str(evidence.get("installed_version", "")).strip()
        installed_active = evidence.get("active") if isinstance(evidence.get("active"), bool) else None
        target_version = details["target_version"]
        update_name = details["update_name"]
        common = {
            "installed_version": installed_version,
            "installed_active": installed_active,
            "preflight_error": error.message,
        }

        if code.endswith("_NOT_INSTALLED"):
            return {
                **common,
                "action": "complete",
                "outcome": MaintenanceRunStatus.skipped.value,
                "stage": "component-not-installed",
                "message": f"{update_name} is no longer installed on this website. No update was performed.",
            }

        if code.endswith("_UPDATE_VERSION_MISMATCH"):
            if installed_version and cls._installed_version_meets_target(installed_version, target_version):
                return {
                    **common,
                    "action": "complete",
                    "outcome": MaintenanceRunStatus.succeeded.value,
                    "stage": "already-updated",
                    "message": (
                        f"{update_name} is already installed in version {installed_version}, which meets the selected "
                        f"target {target_version}. No update was required."
                    ),
                }
            if installed_version:
                return {
                    **common,
                    "action": "retry",
                    "current_version": installed_version,
                    "message": (
                        f"Bridge found {update_name} in version {installed_version}; retrying with that current version "
                        "and its final package check."
                    ),
                }
            return cls._bridge_update_unavailable_resolution(details, error)

        if code.endswith("_UPDATE_OFFER_CHANGED"):
            if installed_version and cls._installed_version_meets_target(installed_version, target_version):
                return {
                    **common,
                    "action": "complete",
                    "outcome": MaintenanceRunStatus.succeeded.value,
                    "stage": "already-updated",
                    "message": (
                        f"{update_name} is already installed in version {installed_version}, which meets the selected "
                        f"target {target_version}. No update was required."
                    ),
                }
            offered_version = str(evidence.get("offered_version", "")).strip()
            if offered_version and evidence.get("package_available") is True and offered_version != target_version:
                return {
                    **common,
                    "action": "retry",
                    "target_version": offered_version,
                    "message": (
                        f"Bridge found a newer available target for {update_name}: {offered_version}. "
                        "Retrying with the fresh target."
                    ),
                }
            if details["update_kind"] == "plugin" and details["update_identifier"].startswith("jet-"):
                return {**common, "action": "activate-crocoblock"}
            return cls._bridge_update_unavailable_resolution(details, error)

        if code == "KOSMOS_BRIDGE_PLUGIN_ACTIVATION_STATE_CHANGED":
            return {
                **common,
                "action": "complete",
                "outcome": MaintenanceRunStatus.skipped.value,
                "stage": "activation-state-changed",
                "message": (
                    f"{update_name} changed activation state after it was selected. No update was performed so the "
                    "current site state remains unchanged."
                ),
            }
        return None

    @staticmethod
    def _bridge_update_unavailable_resolution(
        details: dict[str, Any],
        error: SiteMcpProxyError,
        *,
        repeated: bool = False,
    ) -> dict[str, Any]:
        installed_version = str(error.details.get("installed_version", "")).strip()
        installed_active = error.details.get("active") if isinstance(error.details.get("active"), bool) else None
        retry_note = " after repeated on-site changes" if repeated else ""
        return {
            "action": "complete",
            "outcome": MaintenanceRunStatus.skipped.value,
            "stage": "update-not-available",
            "message": (
                f"{details['update_name']} is currently installed in version {installed_version or 'unknown'}, but its "
                f"selected update package is no longer available{retry_note}. No update was performed."
            ),
            "installed_version": installed_version,
            "installed_active": installed_active,
            "preflight_error": error.message,
        }

    @staticmethod
    def _adopt_bridge_update_preflight_values(
        run: MaintenanceRun,
        details: dict[str, Any],
        resolution: dict[str, Any],
    ) -> None:
        """Keep the original selection while retrying with the version evidence just returned by the Bridge."""
        refreshed_values: dict[str, Any] = {
            "bridge_final_preflight": True,
            "bridge_final_preflight_retries": int((run.result_json or {}).get("bridge_final_preflight_retries", 0)) + 1,
        }
        current_version = resolution.get("current_version")
        if isinstance(current_version, str) and current_version and current_version != details["current_version"]:
            refreshed_values["selected_current_version"] = details["current_version"]
            refreshed_values["current_version"] = current_version
            refreshed_values["current_version_refreshed"] = True
            details["current_version"] = current_version
        target_version = resolution.get("target_version")
        if isinstance(target_version, str) and target_version and target_version != details["target_version"]:
            refreshed_values["selected_target_version"] = details["target_version"]
            refreshed_values["target_version"] = target_version
            refreshed_values["target_version_refreshed"] = True
            details["target_version"] = target_version
        run.result_json = {**(run.result_json or {}), **refreshed_values}

    def _direct_plugin_update_preflight(self, run: MaintenanceRun, details: dict[str, Any]) -> tuple[str | None, str]:
        """Use the freshly confirmed WordPress offer as the authoritative update scope."""
        current_entry = self._current_plugin_update_entry(run, details)
        if current_entry is None:
            return (
                f"{details['update_name']} is no longer listed as an available {details['update_kind']} update.",
                "",
            )
        if details["update_kind"] == "plugin" and current_entry.is_active is not details["expected_active"]:
            return (
                f"{details['update_name']} changed activation state since it was selected. Refresh the workbench and start a new run.",
                "",
            )

        scope_error = self._direct_plugin_update_scope_error(current_entry)
        if scope_error:
            return scope_error, ""

        refreshed_values: dict[str, Any] = {}
        notes: list[str] = []
        if current_entry.current_version != details["current_version"]:
            selected_current_version = details["current_version"]
            details["current_version"] = current_entry.current_version
            refreshed_values.update(
                {
                    "selected_current_version": selected_current_version,
                    "current_version": current_entry.current_version,
                    "current_version_refreshed": True,
                }
            )
            notes.append(
                f"The confirmed installed version changed from {selected_current_version} to "
                f"{current_entry.current_version}"
            )
        if current_entry.target_version != details["target_version"]:
            selected_target_version = details["target_version"]
            details["target_version"] = current_entry.target_version
            refreshed_values.update(
                {
                    "selected_target_version": selected_target_version,
                    "target_version": current_entry.target_version,
                    "target_version_refreshed": True,
                }
            )
            notes.append(
                f"the available target changed from {selected_target_version} to {current_entry.target_version}"
            )
        if not refreshed_values:
            return None, ""
        run.result_json = {**(run.result_json or {}), **refreshed_values}
        return None, f" {'; '.join(notes)}. The fresh versions will be used."

    def _reconcile_direct_plugin_preflight_mismatch(
        self,
        run: MaintenanceRun,
        details: dict[str, Any],
        preflight_step: MaintenanceRunStep | None,
        preflight_error: str,
    ) -> str | None:
        """Read the live plugin state when the fresh offer no longer matches the selection."""
        if details["update_kind"] != "plugin":
            return None

        self._start_plugin_update_step(
            run,
            preflight_step,
            "The selected update no longer matches the fresh offer. Reading the installed plugin version directly from WordPress.",
        )
        try:
            payload = self.proxy.execute_readonly_ability(
                run.site_id,
                self.LIST_INSTALLED_PLUGINS_ABILITY,
                None,
                timeout_seconds=45,
            )
        except SiteMcpProxyError as exc:
            self._fail_plugin_update_run(
                run,
                f"{preflight_error} The installed plugin version could not be read: {exc.message}",
            )
            return "failed"

        current_plugins = self._result_from_payload(payload).get("plugins", [])
        plugins = current_plugins if isinstance(current_plugins, list) else []
        installed = next(
            (
                plugin
                for plugin in plugins
                if isinstance(plugin, dict) and plugin.get("plugin_file") == details["update_identifier"]
            ),
            None,
        )
        resolution = self._direct_plugin_live_preflight_resolution(details, installed, preflight_error)
        self._finish_direct_plugin_preflight_resolution(run, preflight_step, details, resolution)
        return str(resolution["outcome"])

    @classmethod
    def _direct_plugin_live_preflight_resolution(
        cls,
        details: dict[str, Any],
        installed: dict[str, Any] | None,
        preflight_error: str,
    ) -> dict[str, Any]:
        """Classify a stale selection from the live installed plugin state."""
        update_name = details["update_name"]
        target_version = details["target_version"]
        if installed is None:
            return {
                "outcome": MaintenanceRunStatus.skipped.value,
                "stage": "plugin-not-installed",
                "message": f"{update_name} is no longer installed on this website. No update was performed.",
                "installed_version": "",
                "installed_active": None,
                "preflight_error": preflight_error,
            }

        installed_version = str(installed.get("version", "")).strip()
        installed_active = installed.get("active") if isinstance(installed.get("active"), bool) else None
        if not installed_version:
            return {
                "outcome": MaintenanceRunStatus.skipped.value,
                "stage": "plugin-version-unavailable",
                "message": f"{update_name} is installed, but WordPress did not report its version. No update was performed.",
                "installed_version": "",
                "installed_active": installed_active,
                "preflight_error": preflight_error,
            }
        if cls._installed_version_meets_target(installed_version, target_version):
            return {
                "outcome": MaintenanceRunStatus.succeeded.value,
                "stage": "already-updated",
                "message": (
                    f"{update_name} is already installed in version {installed_version}, which meets the selected target "
                    f"{target_version}. No update was required."
                ),
                "installed_version": installed_version,
                "installed_active": installed_active,
                "preflight_error": preflight_error,
            }
        return {
            "outcome": MaintenanceRunStatus.skipped.value,
            "stage": "update-not-available",
            "message": (
                f"{update_name} is currently installed in version {installed_version}, but the selected update cannot be "
                f"performed: {preflight_error}"
            ),
            "installed_version": installed_version,
            "installed_active": installed_active,
            "preflight_error": preflight_error,
        }

    @staticmethod
    def _installed_version_meets_target(installed_version: str, target_version: str) -> bool:
        """Compare plain numeric WordPress versions without treating pre-releases as newer."""
        installed = installed_version.strip()
        target = target_version.strip()
        if installed == target:
            return True
        numeric_version = re.compile(r"\d+(?:\.\d+)*")
        if not numeric_version.fullmatch(installed) or not numeric_version.fullmatch(target):
            return False
        installed_parts = [int(part) for part in installed.split(".")]
        target_parts = [int(part) for part in target.split(".")]
        length = max(len(installed_parts), len(target_parts))
        installed_parts.extend([0] * (length - len(installed_parts)))
        target_parts.extend([0] * (length - len(target_parts)))
        return tuple(installed_parts) >= tuple(target_parts)

    def _finish_direct_plugin_preflight_resolution(
        self,
        run: MaintenanceRun,
        preflight_step: MaintenanceRunStep | None,
        details: dict[str, Any],
        resolution: dict[str, Any],
        *,
        update_step: MaintenanceRunStep | None = None,
    ) -> None:
        """Persist a safe terminal result without issuing a follow-up full site scan."""
        message = str(resolution["message"])
        observed_version = str(resolution.get("installed_version", "")).strip()
        observed_active = resolution.get("installed_active")
        if observed_version:
            self._record_confirmed_direct_update(
                run,
                details,
                {"installed_version": observed_version, "active": observed_active},
            )
        else:
            # The Bridge proved this stored offer is obsolete even though no component version was returned.
            SiteUpdateService(db=self.db, cipher=self.cipher).record_confirmed_direct_update(
                site_id=run.site_id,
                update_kind=details["update_kind"],
                identifier=details["update_identifier"],
            )

        completed_at = datetime.now(UTC)
        final_message = message
        run.status = str(resolution["outcome"])
        run.completed_at = completed_at
        run.last_checked_at = completed_at
        run.error_message = None
        run.result_json = {
            **(run.result_json or {}),
            "stage": resolution["stage"],
            "stage_message": final_message,
            "installed_version": observed_version,
            "installed_active": resolution.get("installed_active"),
            "preflight_error": resolution["preflight_error"],
            "update_request_skipped": True,
        }
        for step in run.steps:
            if step is preflight_step:
                step.status = MaintenanceRunStepStatus.succeeded.value
                step.completed_at = completed_at
                step.detail = final_message
                step.result_json = {
                    "installed_version": observed_version,
                    "installed_active": resolution.get("installed_active"),
                }
            elif step.status in {MaintenanceRunStepStatus.waiting.value, MaintenanceRunStepStatus.running.value}:
                step.status = MaintenanceRunStepStatus.skipped.value
                step.completed_at = completed_at
                step.detail = "Not run because the final on-site Bridge check resolved this selected update."
        write_audit_log(
            self.db,
            site=run.site,
            actor="kosmos-hub",
            source="hub-worker",
            action="complete-direct-update-run",
            result=run.status,
            detail=f"Direct update run {run.id}: {final_message}",
            request_id=self._plugin_update_batch_id(run),
        )
        self.db.commit()

    def _activate_crocoblock_license_if_required(
        self,
        run: MaintenanceRun,
        details: dict[str, Any],
        preflight_step: MaintenanceRunStep | None,
        *,
        force: bool = False,
        refresh_authorized_offer: bool = True,
    ) -> str | None:
        if details["update_kind"] != "plugin" or not details["update_identifier"].startswith("jet-"):
            return None
        entry = self._current_plugin_update_entry(run, details)
        if not force and (entry is None or not self._is_crocoblock_entry(entry) or entry.execution_ready):
            return None

        self._start_plugin_update_step(
            run,
            preflight_step,
            "Crocoblock needs the stored Hub license before its update package can be requested.",
        )
        try:
            activation = CrocoblockLicenseService(db=self.db, cipher=self.cipher).activate_for_plugin_update(
                actor=run.requested_by or "kosmos-hub",
                site_id=run.site_id,
            )
        except CrocoblockLicenseError as exc:
            return f"{details['update_name']} needs Crocoblock license activation: {exc}"

        run.result_json = {
            **(run.result_json or {}),
            "crocoblock_license_activation_attempted": True,
            "crocoblock_license_activated": True,
            "crocoblock_update_package_ready": activation["update_package_ready"],
        }
        if activation["update_package_ready"] is not True:
            return f"Crocoblock license activation was verified, but {details['update_name']} still has no authorized update package."

        if not refresh_authorized_offer:
            self._start_plugin_update_step(
                run,
                preflight_step,
                "Crocoblock license activation was verified. The Bridge will now request its authorized package directly.",
            )
            return None

        self._start_plugin_update_step(
            run,
            preflight_step,
            "Crocoblock license activation was verified. Refreshing its authorized update package.",
        )
        try:
            SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(run.site_id)
        except SiteMcpProxyError as exc:
            return f"Crocoblock license activation succeeded, but its update package could not be refreshed: {exc.message}"
        return None

    def _current_plugin_update_entry(self, run: MaintenanceRun, details: dict[str, Any]) -> UpdateWorkbenchEntry | None:
        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        entries = inventory.build_update_workbench(inventory.list_items(limit=1000))
        return next(
            (
                entry
                for entry in entries
                if entry.site.id == run.site_id
                and entry.kind == details["update_kind"]
                and entry.identifier == details["update_identifier"]
            ),
            None,
        )

    @staticmethod
    def _selection_scope_error(
        selected_entries: list[UpdateWorkbenchEntry],
        *,
        expected_site_id: int | None,
    ) -> str | None:
        if expected_site_id is not None and any(entry.site.id != expected_site_id for entry in selected_entries):
            return "Select updates from this customer site only."
        return None

    @staticmethod
    def _direct_plugin_update_scope_error(
        entry: UpdateWorkbenchEntry,
        *,
        allow_stored_crocoblock_license: bool = False,
        has_stored_crocoblock_license: bool = False,
    ) -> str | None:
        if entry.kind == "wordpress":
            if entry.identifier != "wordpress-core":
                return "WordPress core does not have a valid update identifier."
            if not entry.current_version or not entry.target_version:
                return "WordPress core does not report both the installed and target version."
            return None
        if entry.kind == "theme":
            if not MaintenanceRunService._is_theme_stylesheet(entry.identifier):
                return f"{entry.name} does not have a valid WordPress theme stylesheet."
            if not entry.current_version or not entry.target_version:
                return f"{entry.name} does not report both the installed and target version."
            return None
        if entry.kind != "plugin":
            return "Direct updates support WordPress core, themes, and plugins only."
        if entry.is_active is None:
            return f"{entry.name} does not report whether it is active or inactive."
        if not MaintenanceRunService._is_plugin_file(entry.identifier):
            return f"{entry.name} does not have a valid WordPress plugin file."
        if not entry.current_version or not entry.target_version:
            return f"{entry.name} does not report both the installed and target version."
        if entry.execution_ready is not True:
            if allow_stored_crocoblock_license and MaintenanceRunService._is_crocoblock_entry(entry):
                if has_stored_crocoblock_license:
                    return None
                return f"{entry.name} needs the centrally stored Crocoblock license before its update package is available."
            return entry.execution_note or f"{entry.name} does not have an authorized update package yet."
        return None

    def _execute_direct_update(self, site_id: int, details: dict[str, Any]) -> dict[str, Any]:
        update_kind = details["update_kind"]
        if update_kind == "plugin":
            return self.proxy.execute_ability(
                site_id,
                self.PLUGIN_UPDATE_ABILITY,
                {
                    "plugin_file": details["update_identifier"],
                    "expected_current_version": details["current_version"],
                    "expected_target_version": details["target_version"],
                    "expected_active": details["expected_active"],
                },
                timeout_seconds=180,
            )
        if update_kind == "theme":
            return self.proxy.execute_ability(
                site_id,
                self.THEME_UPDATE_ABILITY,
                {
                    "stylesheet": details["update_identifier"],
                    "expected_current_version": details["current_version"],
                    "expected_target_version": details["target_version"],
                },
                timeout_seconds=240,
            )
        return self.proxy.execute_ability(
            site_id,
            self.WORDPRESS_CORE_UPDATE_ABILITY,
            {
                "expected_current_version": details["current_version"],
                "expected_target_version": details["target_version"],
            },
            timeout_seconds=300,
        )

    @staticmethod
    def _direct_update_result_error(details: dict[str, Any], result: dict[str, Any]) -> str | None:
        if result.get("updated") is not True or result.get("installed_version") != details["target_version"]:
            return f"{details['update_name']} did not return the selected installed version."
        if details["update_kind"] == "plugin":
            if result.get("plugin_file") != details["update_identifier"] or result.get("active") is not details["expected_active"]:
                return f"{details['update_name']} did not preserve the selected activation state after its update."
        elif details["update_kind"] == "theme":
            if result.get("stylesheet") != details["update_identifier"]:
                return f"{details['update_name']} did not return the selected theme after its update."
        elif result.get("component") != "wordpress-core":
            return "WordPress core did not return the expected update component."
        return None

    @staticmethod
    def _plugin_installation_result_error(details: dict[str, Any], result: dict[str, Any]) -> str | None:
        if result.get("installed") is not True:
            return f"{details['plugin_name']} was not confirmed as installed by WordPress."
        if result.get("plugin_file") != details["plugin_file"]:
            return f"{details['plugin_name']} was installed under an unexpected plugin file."
        if result.get("installed_version") != details["target_version"]:
            return f"{details['plugin_name']} did not return the checked package version."
        if details["activate"] and result.get("active") is not True:
            return f"{details['plugin_name']} was installed, but WordPress did not confirm activation."
        return None

    @staticmethod
    def _direct_update_verification_detail(details: dict[str, Any], result: dict[str, Any] | None = None) -> str:
        reconciled = isinstance(result, dict) and result.get("reconciled_after_update_error") is True
        if details["update_kind"] == "plugin":
            detail = (
                f"Bridge verified {details['update_name']} {details['target_version']} "
                f"{'active' if details['expected_active'] else 'inactive'}."
            )
            if reconciled:
                return (
                    f"{detail} The Bridge reported an update error after the package operation; "
                    "the Hub confirmed the planned version and activation state directly from WordPress."
                )
            return detail
        if details["update_kind"] == "theme":
            return f"Bridge verified theme {details['update_name']} {details['target_version']}."
        return f"Bridge verified WordPress {details['target_version']} on disk."

    @staticmethod
    def _direct_update_reconciliation_note(result: dict[str, Any]) -> str:
        if result.get("reconciled_after_update_error") is not True:
            return ""
        return (
            "The Bridge reported an error after the package operation; the Hub confirmed the selected "
            "version and activation state directly from WordPress. The original Bridge error remains in this run record."
        )

    @staticmethod
    def _update_step_key(update_kind: str) -> str:
        return {
            "plugin": "update-plugin",
            "theme": "update-theme",
            "wordpress": "update-wordpress-core",
        }[update_kind]

    @staticmethod
    def _is_crocoblock_entry(entry: UpdateWorkbenchEntry) -> bool:
        return entry.identifier.startswith("jet-")

    def _has_stored_crocoblock_license(self) -> bool:
        config = CrocoblockLicenseService(db=self.db, cipher=self.cipher).get_config()
        return config is not None and config.enabled

    @staticmethod
    def _plugin_update_health_error(result: object) -> str | None:
        if not isinstance(result, dict):
            return "the Bridge did not return a verifiable health result"
        if result.get("home_healthy") is not True:
            return f"the public homepage health check did not pass (HTTP {MaintenanceRunService._health_status(result.get('home_status'))})"
        if result.get("rest_healthy") is not True:
            return f"the WordPress REST API health check did not pass (HTTP {MaintenanceRunService._health_status(result.get('rest_status'))})"
        if (
            "admin_ajax_healthy" in result
            and result.get("admin_ajax_healthy") is not True
            and not MaintenanceRunService._admin_ajax_access_is_ignored(result)
        ):
            return f"the WordPress admin AJAX health check did not pass (HTTP {MaintenanceRunService._health_status(result.get('admin_ajax_status'))})"
        return None

    @staticmethod
    def _plugin_update_health_detail(result: object) -> str:
        if not isinstance(result, dict):
            return "Post-update health check returned no verifiable result."
        detail = (
            "Post-update health check: "
            f"homepage HTTP {MaintenanceRunService._health_status(result.get('home_status'))}; "
            f"WordPress REST API HTTP {MaintenanceRunService._health_status(result.get('rest_status'))}."
        )
        if MaintenanceRunService._admin_ajax_access_is_ignored(result):
            return f"{detail[:-1]}; WordPress admin AJAX returned HTTP 403 and is blocked by an access policy."
        if "admin_ajax_status" in result:
            return f"{detail[:-1]}; WordPress admin AJAX HTTP {MaintenanceRunService._health_status(result.get('admin_ajax_status'))}."
        return detail

    @staticmethod
    def _post_update_health_failure_kind(result: object) -> str | None:
        if not isinstance(result, dict):
            return None
        if "home_healthy" not in result or "rest_healthy" not in result:
            return "unverified"
        if result.get("home_healthy") is not True:
            return "homepage"
        if result.get("rest_healthy") is not True:
            return "rest-api"
        if (
            "admin_ajax_healthy" in result
            and result.get("admin_ajax_healthy") is not True
            and not MaintenanceRunService._admin_ajax_access_is_ignored(result)
        ):
            return "admin-ajax"
        return None

    @staticmethod
    def _admin_ajax_access_is_ignored(result: object) -> bool:
        """A 403 is normally a local access policy, not an application health failure."""
        return (
            isinstance(result, dict)
            and result.get("admin_ajax_healthy") is False
            and result.get("admin_ajax_status") == 403
        )

    @staticmethod
    def _post_update_health_failure_label(kind: str) -> str:
        return {
            "homepage": "public homepage",
            "rest-api": "WordPress REST API",
            "admin-ajax": "WordPress admin AJAX",
            "unverified": "post-update health verification",
        }.get(kind, "post-update health")

    def _start_plugin_update_step(
        self,
        run: MaintenanceRun,
        step: MaintenanceRunStep | None,
        detail: str,
    ) -> None:
        now = datetime.now(UTC)
        run.last_checked_at = now
        run.result_json = {
            **(run.result_json or {}),
            "stage": step.step_key if step is not None else "running",
            "stage_message": detail,
        }
        if step is not None:
            step.status = MaintenanceRunStepStatus.running.value
            step.started_at = now
            step.detail = detail
        self.db.commit()

    def _complete_plugin_update_step(
        self,
        run: MaintenanceRun,
        step: MaintenanceRunStep | None,
        detail: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        run.last_checked_at = now
        if step is not None:
            step.status = MaintenanceRunStepStatus.succeeded.value
            step.completed_at = now
            step.detail = detail
            step.result_json = dict(result or {})
        self.db.commit()

    def _fail_plugin_update_run(
        self,
        run: MaintenanceRun,
        message: str,
        *,
        health_step: MaintenanceRunStep | None = None,
    ) -> None:
        completed_at = datetime.now(UTC)
        run.status = MaintenanceRunStatus.failed.value
        run.completed_at = completed_at
        run.last_checked_at = completed_at
        run.error_message = message
        run.result_json = {
            **(run.result_json or {}),
            "stage": "failed",
            "stage_message": message,
        }
        target_step = health_step or next(
            (step for step in run.steps if step.status == MaintenanceRunStepStatus.running.value),
            None,
        ) or self._find_step(run, "preflight")
        for step in run.steps:
            if step is target_step:
                step.status = MaintenanceRunStepStatus.failed.value
                step.completed_at = completed_at
                step.detail = message
            elif step.status == MaintenanceRunStepStatus.waiting.value:
                step.status = MaintenanceRunStepStatus.skipped.value
                step.completed_at = completed_at
                step.detail = "Not run because this direct update failed."
        action = "plugin-installation-run" if run.kind == self.PLUGIN_INSTALLATION_KIND else "direct-plugin-update-run"
        label = "plugin installation" if run.kind == self.PLUGIN_INSTALLATION_KIND else "direct update"
        write_audit_log(
            self.db,
            site=run.site,
            actor="kosmos-hub",
            source="hub-worker",
            action=action,
            result="failed",
            detail=f"{label.title()} run {run.id} failed: {message}",
            request_id=self._plugin_update_batch_id(run),
        )
        self.db.commit()

    def _direct_update_batch_runs(self, batch_id: str) -> list[MaintenanceRun]:
        statement = select(MaintenanceRun).where(MaintenanceRun.kind == self.PLUGIN_UPDATE_KIND)
        runs = [
            run
            for run in self.db.scalars(statement)
            if self._plugin_update_batch_id(run) == batch_id
        ]
        return sorted(runs, key=self._batch_position)

    @classmethod
    def _has_direct_update_failure_streak(cls, runs: list[MaintenanceRun]) -> bool:
        failures = 0
        for run in runs:
            if run.status == MaintenanceRunStatus.failed.value:
                failures += 1
                if failures >= cls.DIRECT_UPDATE_FAILURE_STREAK_LIMIT:
                    return True
                continue
            if run.status in {MaintenanceRunStatus.succeeded.value, MaintenanceRunStatus.skipped.value}:
                failures = 0
                continue
            # A preceding update is still being processed, so later outcomes do not yet form a sequence.
            break
        return False

    @staticmethod
    def _batch_position(run: MaintenanceRun) -> tuple[int, int]:
        value = (run.result_json or {}).get("batch_position")
        try:
            position = int(value)
        except (TypeError, ValueError):
            position = run.id
        return position, run.id

    def _skip_queued_maintenance_runs(self, batch_id: str, *, kind: str, message: str) -> int:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(
                MaintenanceRun.kind == kind,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.id.asc())
        )
        skipped = 0
        completed_at = datetime.now(UTC)
        for run in self.db.scalars(statement):
            if self._plugin_update_batch_id(run) != batch_id:
                continue
            if (run.result_json or {}).get("stage") != "queued":
                continue
            run.status = MaintenanceRunStatus.skipped.value
            run.completed_at = completed_at
            run.last_checked_at = completed_at
            run.error_message = message
            run.result_json = {
                **(run.result_json or {}),
                "stage": "skipped",
                "stage_message": message,
            }
            for step in run.steps:
                if step.status in {MaintenanceRunStepStatus.waiting.value, MaintenanceRunStepStatus.running.value}:
                    step.status = MaintenanceRunStepStatus.skipped.value
                    step.completed_at = completed_at
                    step.detail = message
            action = "plugin-installation-run" if kind == self.PLUGIN_INSTALLATION_KIND else "direct-plugin-update-run"
            write_audit_log(
                self.db,
                site=run.site,
                actor="kosmos-hub",
                source="hub-worker",
                action=action,
                result="skipped",
                detail=f"Direct update run {run.id} skipped: {message}",
                request_id=batch_id,
            )
            skipped += 1
        if skipped:
            self.db.commit()
        return skipped

    @staticmethod
    def _plugin_update_batch_id(run: MaintenanceRun) -> str | None:
        batch_id = (run.result_json or {}).get("batch_id")
        return batch_id if isinstance(batch_id, str) and re.fullmatch(r"[a-f0-9]{32}", batch_id) else None

    @staticmethod
    def _direct_update_details(run: MaintenanceRun) -> dict[str, Any] | None:
        values = run.result_json or {}
        if not isinstance(values, dict):
            return None
        update_kind = values.get("update_kind", "plugin")
        details = {
            "update_identifier": values.get("update_identifier", values.get("plugin_file")),
            "update_name": values.get("update_name", values.get("plugin_name")),
            "current_version": values.get("current_version"),
            "target_version": values.get("target_version"),
        }
        if update_kind not in {"plugin", "theme", "wordpress"}:
            return None
        if not all(isinstance(value, str) and value.strip() for value in details.values()):
            return None
        identifier = details["update_identifier"].strip()
        if update_kind == "plugin" and not MaintenanceRunService._is_plugin_file(identifier):
            return None
        if update_kind == "theme" and not MaintenanceRunService._is_theme_stylesheet(identifier):
            return None
        if update_kind == "wordpress" and identifier != "wordpress-core":
            return None
        expected_active = values.get("expected_active")
        if update_kind == "plugin" and not isinstance(expected_active, bool):
            return None
        return {
            **{key: value.strip() for key, value in details.items()},
            "update_kind": update_kind,
            "expected_active": expected_active,
        }

    def _plugin_installation_details(self, run: MaintenanceRun) -> dict[str, Any] | None:
        values = run.result_json or {}
        if not isinstance(values, dict) or not isinstance(run.plugin_installation_package_id, int):
            return None
        package = self.db.get(PluginInstallationPackage, run.plugin_installation_package_id)
        if package is None:
            return None
        if package.expires_at is not None and package.expires_at < datetime.now(UTC):
            return None
        if (
            not self._is_plugin_file(package.plugin_file)
            or not package.plugin_name
            or not package.plugin_version
            or re.fullmatch(r"[a-f0-9]{64}", package.sha256) is None
            or values.get("plugin_file") != package.plugin_file
            or values.get("target_version") != package.plugin_version
        ):
            return None
        activate = values.get("activate")
        replace_existing = values.get("replace_existing")
        if not isinstance(activate, bool) or not isinstance(replace_existing, bool):
            return None
        return {
            "package_id": package.id,
            "plugin_name": package.plugin_name,
            "plugin_file": package.plugin_file,
            "target_version": package.plugin_version,
            "package_sha256": package.sha256,
            "activate": activate,
            "replace_existing": replace_existing,
        }

    @staticmethod
    def _is_plugin_file(identifier: str | None) -> bool:
        return isinstance(identifier, str) and re.fullmatch(
            r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*\.php",
            identifier,
        ) is not None

    @staticmethod
    def _is_theme_stylesheet(identifier: str | None) -> bool:
        return isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", identifier) is not None

    @staticmethod
    def _health_status(value: object) -> str:
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else "not reported"

    def poll_active_updraftplus_backups(self, *, limit: int = 25) -> dict[str, int]:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(
                MaintenanceRun.kind.in_((self.UPDRAFT_BACKUP_KIND, self.UPDRAFT_BACKUP_DELETE_KIND)),
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.asc())
            .limit(limit)
        )
        runs = list(self.db.scalars(statement))
        summary = {"checked": 0, "succeeded": 0, "failed": 0, "waiting": 0}
        for run in runs:
            summary["checked"] += 1
            outcome = (
                self._poll_updraftplus_backup_deletion(run)
                if run.kind == self.UPDRAFT_BACKUP_DELETE_KIND
                else self._poll_updraftplus_backup(run)
            )
            summary[outcome] += 1
        return summary

    def _poll_updraftplus_backup(self, run: MaintenanceRun) -> str:
        now = datetime.now(UTC)
        if not self._is_backup_nonce(run.bridge_backup_nonce):
            self._fail_run(run, actor="kosmos-hub", message="The backup run has no valid UpdraftPlus backup identifier.")
            return "failed"

        if self._backup_was_verified(run):
            return (
                self._poll_updraftplus_backup_cleanup(run)
                if self._backup_cleanup_requested(run)
                else self._complete_updraftplus_backup_after_verification(run)
            )

        if now - self._as_utc(run.started_at) > self.BACKUP_TIMEOUT:
            self._fail_run(
                run,
                actor="kosmos-hub",
                message="UpdraftPlus did not record the requested complete protected backup within three minutes.",
            )
            return "failed"

        verification_step = self._find_step(run, "verify-backup")
        if verification_step is not None and verification_step.status == MaintenanceRunStepStatus.waiting.value:
            verification_step.status = MaintenanceRunStepStatus.running.value

        try:
            payload = self.proxy.execute_readonly_ability(
                run.site_id,
                self.BACKUP_STATUS_ABILITY,
                {"backup_nonce": run.bridge_backup_nonce},
                timeout_seconds=30,
            )
        except SiteMcpProxyError as exc:
            self._mark_waiting(run, verification_step, f"Backup verification will retry: {exc.message}")
            return "waiting"

        result = self._result_from_payload(payload)
        run.last_checked_at = now
        bridge_status = self._bridge_backup_status(result)
        bridge_message = self._bridge_backup_message(result, bridge_status)
        run.result_json = {
            **(run.result_json or {}),
            "bridge_status": bridge_status,
            "bridge_status_message": bridge_message,
        }
        if result.get("installed") is not True or result.get("active") is not True:
            self._fail_run(run, actor="kosmos-hub", message="UpdraftPlus is no longer installed and active on this site.")
            return "failed"

        if bridge_status == "failed":
            self._fail_run(run, actor="kosmos-hub", message=bridge_message)
            return "failed"

        if self._is_complete_requested_backup(result, run.bridge_backup_nonce) and result.get("retention_protected") is not True:
            self._fail_run(
                run,
                actor="kosmos-hub",
                message="UpdraftPlus recorded the requested backup, but it is not protected from automatic deletion.",
            )
            return "failed"

        if self._is_verified_backup_result(result, run.bridge_backup_nonce):
            snapshot = SiteBackupService(db=self.db, cipher=self.cipher).store_backup_status_result(run.site_id, result)["snapshot"]
            run.result_json = {
                **(run.result_json or {}),
                "provider": "updraftplus",
                "backup_nonce": run.bridge_backup_nonce,
                "backup_at": snapshot.backup_at.isoformat() if snapshot.backup_at else None,
                "components": snapshot.components_json,
                "retention_protected": True,
                "backup_verified": True,
                "bridge_status": "cleanup" if self._backup_cleanup_requested(run) else "completed",
                "bridge_status_message": (
                    "The protected backup was verified. The Hub is now checking the oldest eligible backup for cleanup."
                    if self._backup_cleanup_requested(run)
                    else "The protected backup was verified without automatic cleanup."
                ),
            }
            if verification_step is not None:
                verification_step.status = MaintenanceRunStepStatus.succeeded.value
                verification_step.completed_at = datetime.now(UTC)
                verification_step.detail = "UpdraftPlus recorded the requested complete backup and its protection from automatic deletion."
                verification_step.result_json = dict(run.result_json)
            self.db.commit()
            return (
                self._poll_updraftplus_backup_cleanup(run)
                if self._backup_cleanup_requested(run)
                else self._complete_updraftplus_backup_after_verification(run)
            )

        self._mark_waiting(
            run,
            verification_step,
            self._backup_waiting_detail(bridge_status, bridge_message),
        )
        return "waiting"

    def _complete_updraftplus_backup_after_verification(self, run: MaintenanceRun) -> str:
        run.status = MaintenanceRunStatus.succeeded.value
        run.completed_at = datetime.now(UTC)
        run.last_checked_at = run.completed_at
        run.result_json = {
            **(run.result_json or {}),
            "bridge_status": "completed",
            "bridge_status_message": "The protected backup was verified without automatic cleanup.",
            "cleanup": {
                "status": "not-requested",
                "message": "Automatic cleanup was not requested for this backup.",
                "backup_sets_removed": 0,
                "local_files_deleted": 0,
                "remote_files_deleted": 0,
            },
        }
        write_audit_log(
            self.db,
            site=run.site,
            actor="kosmos-hub",
            source="hub-worker",
            action="complete-updraftplus-backup-run",
            result="succeeded",
            detail=f"Maintenance run {run.id} verified a fresh protected backup without cleanup.",
        )
        self.db.commit()
        return "succeeded"

    def _poll_updraftplus_backup_deletion(self, run: MaintenanceRun) -> str:
        steps = sorted(run.steps, key=lambda step: step.id)
        pending_step = next(
            (
                step
                for step in steps
                if step.status in {MaintenanceRunStepStatus.waiting.value, MaintenanceRunStepStatus.running.value}
            ),
            None,
        )
        if pending_step is None:
            return self._complete_updraftplus_backup_deletion(run, steps)

        target = dict(pending_step.result_json or {})
        backup_nonce = target.get("backup_nonce")
        backup_timestamp = target.get("backup_timestamp")
        if not self._is_backup_nonce(backup_nonce) or not isinstance(backup_timestamp, int) or backup_timestamp <= 0:
            self._fail_updraftplus_backup_deletion_step(
                pending_step,
                "The selected backup has no valid UpdraftPlus identity.",
            )
            return "waiting"

        if pending_step.status == MaintenanceRunStepStatus.waiting.value:
            pending_step.status = MaintenanceRunStepStatus.running.value

        if datetime.now(UTC) - self._as_utc(pending_step.started_at) > self.BACKUP_TIMEOUT:
            self._fail_updraftplus_backup_deletion_step(
                pending_step,
                "UpdraftPlus did not finish deleting the selected backup within three minutes.",
            )
            return "waiting"

        if target.get("deletion_status") == "verifying":
            return self._verify_updraftplus_backup_deletion_step(run, pending_step, target)

        try:
            payload = self.proxy.execute_ability(
                run.site_id,
                self.DELETE_BACKUP_ABILITY,
                {
                    "backup_nonce": backup_nonce,
                    "backup_timestamp": backup_timestamp,
                    "delete_remote": True,
                    "allow_protected_delete": True,
                },
                timeout_seconds=self.DELETE_BACKUP_TIMEOUT_SECONDS,
            )
        except SiteMcpProxyError as exc:
            self._fail_updraftplus_backup_deletion_step(pending_step, exc.message)
            return "waiting"

        result = self._result_from_payload(payload)
        message = self._safe_message(result.get("message"), "UpdraftPlus did not return a deletion result.")
        target = {
            **target,
            "deletion_status": self._safe_string(result.get("status")) or "failed",
            "backup_sets_removed": self._non_negative_int(result.get("backup_sets_removed")),
            "local_files_deleted": self._non_negative_int(result.get("local_files_deleted")),
            "remote_files_deleted": self._non_negative_int(result.get("remote_files_deleted")),
            "message": message,
        }
        pending_step.result_json = target
        if target["deletion_status"] == "completed" and result.get("completed") is True:
            pending_step.detail = "UpdraftPlus reported deletion. The Hub is checking remote storage."
            pending_step.result_json = {**target, "deletion_status": "verifying"}
            self._mark_waiting(run, pending_step, pending_step.detail)
            return "waiting"
        if target["deletion_status"] == "running":
            pending_step.detail = message
            self._mark_waiting(run, pending_step, message)
            return "waiting"

        self._fail_updraftplus_backup_deletion_step(pending_step, message)
        return "waiting"

    def _verify_updraftplus_backup_deletion_step(
        self,
        run: MaintenanceRun,
        step: MaintenanceRunStep,
        target: dict[str, Any],
    ) -> str:
        try:
            payload = self.proxy.execute_readonly_ability(
                run.site_id,
                self.VERIFY_BACKUP_DELETION_ABILITY,
                {
                    "backup_nonce": target["backup_nonce"],
                    "backup_timestamp": target["backup_timestamp"],
                },
                timeout_seconds=self.REMOTE_DELETION_VERIFICATION_TIMEOUT_SECONDS,
            )
        except SiteMcpProxyError as exc:
            self._mark_waiting(run, step, f"Backup deletion verification will retry: {exc.message}")
            return "waiting"

        result = self._result_from_payload(payload)
        message = self._safe_message(result.get("message"), "UpdraftPlus did not return a remote deletion verification result.")
        if result.get("verified") is True:
            step.status = MaintenanceRunStepStatus.succeeded.value
            step.completed_at = datetime.now(UTC)
            step.detail = message
            step.result_json = {**target, "deletion_status": "completed", "remote_deletion_verified": True, "message": message}
            self.db.commit()
            return "waiting"

        remaining_components = result.get("remaining_components")
        components = ", ".join(component for component in remaining_components if isinstance(component, str)) if isinstance(remaining_components, list) else ""
        detail = f" Remaining components: {components}." if components else ""
        self._fail_updraftplus_backup_deletion_step(
            step,
            f"Backup deletion was not confirmed by the UpdraftPlus remote rescan: {message}{detail}",
        )
        return "waiting"

    def _fail_updraftplus_backup_deletion_step(self, step: MaintenanceRunStep, message: str) -> None:
        step.status = MaintenanceRunStepStatus.failed.value
        step.completed_at = datetime.now(UTC)
        step.detail = message
        step.result_json = {**(step.result_json or {}), "deletion_status": "failed", "message": message}
        self.db.commit()

    def _complete_updraftplus_backup_deletion(
        self,
        run: MaintenanceRun,
        steps: list[MaintenanceRunStep],
    ) -> str:
        failed_steps = [step for step in steps if step.status == MaintenanceRunStepStatus.failed.value]
        completed_at = datetime.now(UTC)
        run.status = MaintenanceRunStatus.failed.value if failed_steps else MaintenanceRunStatus.succeeded.value
        run.completed_at = completed_at
        run.last_checked_at = completed_at
        refresh_error = ""
        try:
            SiteBackupService(db=self.db, cipher=self.cipher).refresh_site_backup_status(run.site_id)
        except SiteMcpProxyError as exc:
            refresh_error = exc.message
        run.result_json = {
            **(run.result_json or {}),
            "completed_count": len(steps) - len(failed_steps),
            "failed_count": len(failed_steps),
            "backup_list_refresh_error": refresh_error or None,
        }
        if failed_steps:
            run.error_message = f"{len(failed_steps)} of {len(steps)} selected backup deletion(s) could not be verified."
        write_audit_log(
            self.db,
            site=run.site,
            actor="kosmos-hub",
            source="hub-worker",
            action="complete-updraftplus-backup-deletion-run",
            result=run.status,
            detail=(
                f"Deleted and verified {len(steps) - len(failed_steps)} of {len(steps)} selected UpdraftPlus backup set(s)."
                if not failed_steps
                else f"Verified {len(steps) - len(failed_steps)} of {len(steps)} selected UpdraftPlus backup deletion(s)."
            ),
        )
        self.db.commit()
        return run.status

    def _poll_updraftplus_backup_cleanup(self, run: MaintenanceRun) -> str:
        cleanup_step = self._find_step(run, "prune-oldest-backup")
        if cleanup_step is None:
            cleanup_step = MaintenanceRunStep(
                run=run,
                step_key="prune-oldest-backup",
                status=MaintenanceRunStepStatus.running.value,
                started_at=datetime.now(UTC),
                detail="Finding the oldest eligible complete backup marked for manual deletion.",
                result_json={},
            )
            self.db.add(cleanup_step)
            self.db.flush()

        cleanup = self._cleanup_result(run)
        if cleanup.get("status") == "verifying":
            return self._verify_updraftplus_backup_cleanup(run, cleanup_step, cleanup)
        if cleanup.get("status") in {"completed", "skipped"}:
            return self._complete_run_after_cleanup(run, cleanup_step, cleanup)

        if not cleanup:
            try:
                payload = self.proxy.execute_readonly_ability(
                    run.site_id,
                    self.LIST_BACKUPS_ABILITY,
                    {},
                    timeout_seconds=30,
                )
            except SiteMcpProxyError as exc:
                self._fail_run(run, actor="kosmos-hub", message=f"Backup cleanup could not list backup sets: {exc.message}")
                return "failed"

            candidate = self._oldest_manually_protected_cleanup_candidate(
                self._result_from_payload(payload).get("backups"),
                run.bridge_backup_nonce,
            )
            if candidate is None:
                cleanup = {
                    "status": "skipped",
                    "message": "No older complete backup marked for manual deletion is available for cleanup.",
                    "backup_sets_removed": 0,
                    "local_files_deleted": 0,
                    "remote_files_deleted": 0,
                }
                self._store_cleanup_result(run, cleanup_step, cleanup)
                return self._complete_run_after_cleanup(run, cleanup_step, cleanup)

            cleanup = {
                "status": "running",
                "backup_nonce": candidate["backup_nonce"],
                "backup_timestamp": candidate["backup_timestamp"],
                "backup_at": candidate["backup_at"],
                "backup_sets_removed": 0,
                "local_files_deleted": 0,
                "remote_files_deleted": 0,
                "message": "Deleting the oldest eligible complete backup marked for manual deletion locally and from the configured remote storage.",
            }
            self._store_cleanup_result(run, cleanup_step, cleanup)

        try:
            payload = self.proxy.execute_ability(
                run.site_id,
                self.DELETE_BACKUP_ABILITY,
                {
                    "backup_nonce": cleanup["backup_nonce"],
                    "backup_timestamp": cleanup["backup_timestamp"],
                    "delete_remote": True,
                    "allow_protected_delete": True,
                },
                timeout_seconds=self.DELETE_BACKUP_TIMEOUT_SECONDS,
            )
        except SiteMcpProxyError as exc:
            self._fail_run(run, actor="kosmos-hub", message=f"Backup cleanup could not start or continue: {exc.message}")
            return "failed"

        result = self._result_from_payload(payload)
        cleanup = {
            **cleanup,
            "status": self._safe_string(result.get("status")) or "failed",
            "backup_sets_removed": int(cleanup.get("backup_sets_removed", 0)) + self._non_negative_int(result.get("backup_sets_removed")),
            "local_files_deleted": int(cleanup.get("local_files_deleted", 0)) + self._non_negative_int(result.get("local_files_deleted")),
            "remote_files_deleted": int(cleanup.get("remote_files_deleted", 0)) + self._non_negative_int(result.get("remote_files_deleted")),
            "message": self._safe_message(result.get("message"), "UpdraftPlus did not return a cleanup result."),
        }
        self._store_cleanup_result(run, cleanup_step, cleanup)

        if cleanup["status"] == "running":
            self._mark_waiting(run, cleanup_step, cleanup["message"])
            return "waiting"
        if cleanup["status"] == "completed" and result.get("completed") is True:
            cleanup["status"] = "verifying"
            cleanup["message"] = "UpdraftPlus reported deletion. The Hub is rescanning configured remote storage before confirming completion."
            self._store_cleanup_result(run, cleanup_step, cleanup)
            self._mark_waiting(run, cleanup_step, cleanup["message"])
            return "waiting"

        self._fail_run(run, actor="kosmos-hub", message=cleanup["message"])
        return "failed"

    def _verify_updraftplus_backup_cleanup(
        self,
        run: MaintenanceRun,
        cleanup_step: MaintenanceRunStep,
        cleanup: dict[str, Any],
    ) -> str:
        try:
            payload = self.proxy.execute_readonly_ability(
                run.site_id,
                self.VERIFY_BACKUP_DELETION_ABILITY,
                {
                    "backup_nonce": cleanup["backup_nonce"],
                    "backup_timestamp": cleanup["backup_timestamp"],
                },
                timeout_seconds=self.REMOTE_DELETION_VERIFICATION_TIMEOUT_SECONDS,
            )
        except SiteMcpProxyError as exc:
            self._fail_run(run, actor="kosmos-hub", message=f"Backup cleanup could not be verified against remote storage: {exc.message}")
            return "failed"

        result = self._result_from_payload(payload)
        message = self._safe_message(result.get("message"), "UpdraftPlus did not return a remote deletion verification result.")
        if result.get("verified") is True:
            cleanup = {
                **cleanup,
                "status": "completed",
                "message": message,
                "remote_deletion_verified": True,
            }
            self._store_cleanup_result(run, cleanup_step, cleanup)
            return self._complete_run_after_cleanup(run, cleanup_step, cleanup)

        remaining_components = result.get("remaining_components")
        components = ", ".join(component for component in remaining_components if isinstance(component, str)) if isinstance(remaining_components, list) else ""
        detail = f" Remaining components: {components}." if components else ""
        self._fail_run(run, actor="kosmos-hub", message=f"Backup cleanup was not confirmed by the UpdraftPlus remote rescan: {message}{detail}")
        return "failed"

    def _complete_run_after_cleanup(
        self,
        run: MaintenanceRun,
        cleanup_step: MaintenanceRunStep,
        cleanup: dict[str, Any],
    ) -> str:
        run.status = MaintenanceRunStatus.succeeded.value
        run.completed_at = datetime.now(UTC)
        run.last_checked_at = run.completed_at
        run.result_json = {
            **(run.result_json or {}),
            "bridge_status": "completed",
            "bridge_status_message": cleanup["message"],
            "cleanup": cleanup,
        }
        cleanup_step.status = MaintenanceRunStepStatus.succeeded.value
        cleanup_step.completed_at = run.completed_at
        cleanup_step.detail = cleanup["message"]
        cleanup_step.result_json = dict(cleanup)
        write_audit_log(
            self.db,
            site=run.site,
            actor="kosmos-hub",
            source="hub-worker",
            action="complete-updraftplus-backup-run",
            result="succeeded",
            detail=(
                f"Maintenance run {run.id} verified a fresh protected backup and "
                f"finished backup cleanup: {cleanup['message']}"
            ),
        )
        self.db.commit()
        return "succeeded"

    def _store_cleanup_result(
        self,
        run: MaintenanceRun,
        cleanup_step: MaintenanceRunStep,
        cleanup: dict[str, Any],
    ) -> None:
        run.result_json = {
            **(run.result_json or {}),
            "cleanup": cleanup,
            "bridge_status": "cleanup",
            "bridge_status_message": cleanup["message"],
        }
        cleanup_step.status = MaintenanceRunStepStatus.running.value
        cleanup_step.detail = cleanup["message"]
        cleanup_step.result_json = dict(cleanup)
        self.db.commit()

    @classmethod
    def _oldest_manually_protected_cleanup_candidate(
        cls,
        backups: object,
        protected_backup_nonce: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(backups, list):
            return None

        normalized_backups: list[dict[str, Any]] = []
        for backup in backups:
            if not isinstance(backup, dict):
                continue
            nonce = backup.get("backup_nonce")
            timestamp = backup.get("backup_timestamp")
            backup_at = backup.get("backup_at")
            if (
                not cls._is_backup_nonce(nonce)
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp <= 0
                or not isinstance(backup_at, str)
                or not backup_at.strip()
            ):
                continue
            normalized = {
                "backup_nonce": nonce,
                "backup_timestamp": timestamp,
                "backup_at": backup_at,
                "complete": backup.get("complete") is True,
                "retention_protected": backup.get("retention_protected") is True,
            }
            normalized_backups.append(normalized)

        protected_backup = next(
            (backup for backup in normalized_backups if backup["backup_nonce"] == protected_backup_nonce),
            None,
        )
        if protected_backup is None:
            return None

        candidates = [
            backup
            for backup in normalized_backups
            if backup["backup_nonce"] != protected_backup_nonce
            and backup["backup_timestamp"] < protected_backup["backup_timestamp"]
            and backup["complete"] is True
            and backup["retention_protected"] is True
        ]
        return min(candidates, key=lambda candidate: candidate["backup_timestamp"]) if candidates else None

    @classmethod
    def _selected_backup_identities(cls, selections: list[str]) -> list[tuple[str, int]]:
        identities: list[tuple[str, int]] = []
        for selection in dict.fromkeys(selection for selection in selections if selection):
            backup_nonce, separator, raw_timestamp = selection.partition(":")
            try:
                backup_timestamp = int(raw_timestamp)
            except (TypeError, ValueError):
                backup_timestamp = 0
            if separator != ":" or not cls._is_backup_nonce(backup_nonce) or backup_timestamp <= 0:
                raise ValueError("One or more selected backups have an invalid identity.")
            identities.append((backup_nonce, backup_timestamp))
        return identities

    @classmethod
    def _selected_backup_targets(
        cls,
        backups: object,
        selected_identities: list[tuple[str, int]],
    ) -> list[dict[str, Any]]:
        if not isinstance(backups, list):
            return []

        available: dict[tuple[str, int], dict[str, Any]] = {}
        for backup in backups:
            if not isinstance(backup, dict):
                continue
            backup_nonce = backup.get("backup_nonce")
            backup_timestamp = backup.get("backup_timestamp")
            backup_at = backup.get("backup_at")
            if (
                not cls._is_backup_nonce(backup_nonce)
                or not isinstance(backup_timestamp, int)
                or isinstance(backup_timestamp, bool)
                or backup_timestamp <= 0
                or not isinstance(backup_at, str)
                or not backup_at.strip()
            ):
                continue
            available[(backup_nonce, backup_timestamp)] = {
                "backup_nonce": backup_nonce,
                "backup_timestamp": backup_timestamp,
                "backup_at": backup_at,
                "complete": backup.get("complete") is True,
                "retention_protected": backup.get("retention_protected") is True,
            }
        return [available[identity] for identity in selected_identities if identity in available]

    @staticmethod
    def _backup_cleanup_requested(run: MaintenanceRun) -> bool:
        # Older queued runs used cleanup by default before the optional no-prune mode existed.
        return (run.result_json or {}).get("cleanup_oldest", True) is True

    @staticmethod
    def _cleanup_result(run: MaintenanceRun) -> dict[str, Any]:
        cleanup = (run.result_json or {}).get("cleanup")
        return dict(cleanup) if isinstance(cleanup, dict) else {}

    def _mark_waiting(self, run: MaintenanceRun, step: MaintenanceRunStep | None, detail: str) -> None:
        run.last_checked_at = datetime.now(UTC)
        if step is not None:
            step.status = MaintenanceRunStepStatus.running.value
            step.detail = detail
        self.db.commit()

    def _fail_run(self, run: MaintenanceRun, *, actor: str, message: str) -> None:
        run.status = MaintenanceRunStatus.failed.value
        run.completed_at = datetime.now(UTC)
        run.last_checked_at = run.completed_at
        run.error_message = message
        cleanup_step = self._find_step(run, "prune-oldest-backup")
        verification_step = self._find_step(run, "verify-backup")
        target_step = cleanup_step or verification_step or self._find_step(run, "request-backup")
        if target_step is not None:
            target_step.status = MaintenanceRunStepStatus.failed.value
            target_step.completed_at = run.completed_at
            target_step.detail = message
        write_audit_log(
            self.db,
            site=run.site,
            actor=actor,
            source="hub-worker" if actor == "kosmos-hub" else "hub-web",
            action="updraftplus-backup-run",
            result="failed",
            detail=f"Maintenance run {run.id} failed: {message}",
        )
        self.db.commit()

    @staticmethod
    def _result_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result", {})
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _safe_message(value: object, fallback: str) -> str:
        return value.strip() if isinstance(value, str) and value.strip() else fallback

    @staticmethod
    def _safe_string(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _non_negative_int(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _bridge_backup_status(cls, result: dict[str, Any]) -> str:
        value = cls._safe_string(result.get("request_status"))
        return value if value in {"queued", "starting", "running", "completed", "failed"} else "queued"

    @classmethod
    def _bridge_backup_message(cls, result: dict[str, Any], bridge_status: str) -> str:
        message = cls._safe_string(result.get("request_message")) or cls._safe_string(result.get("message"))
        if message:
            return message

        return {
            "queued": "The Bridge queued the protected backup for the WordPress background worker.",
            "starting": "The WordPress background worker is starting the protected backup with UpdraftPlus.",
            "running": "UpdraftPlus is writing the protected backup to the configured destination.",
            "completed": "UpdraftPlus recorded the requested protected backup.",
            "failed": "The Bridge could not start the protected backup.",
        }[bridge_status]

    @classmethod
    def _backup_waiting_detail(cls, bridge_status: str, bridge_message: str) -> str:
        if bridge_status == "queued":
            return f"{bridge_message} The Hub is waiting for WordPress to run it."
        if bridge_status == "starting":
            return f"{bridge_message} The Hub will verify the backup record automatically."
        if bridge_status == "running":
            return f"{bridge_message} The Hub is waiting for the complete protected backup record."
        return f"{bridge_message} The Hub will check again automatically."

    @staticmethod
    def _is_backup_nonce(value: object) -> bool:
        return isinstance(value, str) and len(value) == 12 and all(character in "0123456789abcdef" for character in value)

    @classmethod
    def _is_verified_backup_result(cls, result: dict[str, Any], backup_nonce: str | None) -> bool:
        return (
            cls._is_complete_requested_backup(result, backup_nonce)
            and result.get("retention_protected") is True
        )

    @staticmethod
    def _is_complete_requested_backup(result: dict[str, Any], backup_nonce: str | None) -> bool:
        return (
            result.get("available") is True
            and result.get("complete") is True
            and result.get("backup_nonce") == backup_nonce
            and isinstance(result.get("latest_backup_at"), str)
            and bool(result["latest_backup_at"].strip())
        )

    @staticmethod
    def _backup_was_verified(run: MaintenanceRun) -> bool:
        return (run.result_json or {}).get("backup_verified") is True

    @staticmethod
    def _find_step(run: MaintenanceRun, step_key: str) -> MaintenanceRunStep | None:
        return next((step for step in run.steps if step.step_key == step_key), None)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _maintenance_history_day_label(day_key: str) -> str:
        return datetime.strptime(day_key, "%Y-%m-%d").strftime("%d.%m.%Y")
