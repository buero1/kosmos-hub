import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import SecretCipher
from app.models.maintenance_run import (
    MaintenanceRun,
    MaintenanceRunStatus,
    MaintenanceRunStep,
    MaintenanceRunStepStatus,
)
from app.models.site import Site, SiteStatus
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
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


class MaintenanceRunService:
    """Run bounded maintenance tasks and persist evidence for later automation."""

    UPDRAFT_BACKUP_KIND = "updraftplus-backup"
    PLUGIN_UPDATE_KIND = "direct-plugin-update"
    START_BACKUP_ABILITY = "kosmos-bridge/start-updraftplus-backup"
    BACKUP_STATUS_ABILITY = "kosmos-bridge/get-updraftplus-backup-status"
    LIST_BACKUPS_ABILITY = "kosmos-bridge/list-updraftplus-backups"
    DELETE_BACKUP_ABILITY = "kosmos-bridge/delete-updraftplus-backup"
    VERIFY_BACKUP_DELETION_ABILITY = "kosmos-bridge/verify-updraftplus-backup-deletion"
    PLUGIN_UPDATE_ABILITY = "kosmos-bridge/update-plugin"
    SITE_HEALTH_ABILITY = "kosmos-bridge/check-site-health"
    START_BACKUP_TIMEOUT_SECONDS = 20
    DELETE_BACKUP_TIMEOUT_SECONDS = 180
    REMOTE_DELETION_VERIFICATION_TIMEOUT_SECONDS = 60
    BACKUP_TIMEOUT = timedelta(minutes=3)

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    def list_site_runs(self, site_id: int, *, limit: int = 8) -> list[MaintenanceRun]:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps))
            .where(MaintenanceRun.site_id == site_id)
            .order_by(MaintenanceRun.started_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement))

    def start_updraftplus_backup(self, *, site_id: int, actor: str) -> MaintenanceRunOutcome:
        site = self.repository.get_site(site_id)
        if site is None:
            raise ValueError("Site not found.")
        if site.status != SiteStatus.verified.value:
            raise ValueError("Only verified sites can start a maintenance run.")

        active_run = self.db.scalar(
            select(MaintenanceRun)
            .where(
                MaintenanceRun.site_id == site_id,
                MaintenanceRun.kind == self.UPDRAFT_BACKUP_KIND,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.desc())
            .limit(1)
        )
        if active_run is not None:
            return MaintenanceRunOutcome(
                run=active_run,
                result="blocked",
                message="A fresh UpdraftPlus backup is already running for this site.",
            )

        now = datetime.now(UTC)
        run = MaintenanceRun(
            site=site,
            kind=self.UPDRAFT_BACKUP_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by=actor,
            started_at=now,
            result_json={},
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
            message="The protected UpdraftPlus backup was queued for immediate background processing. The Hub will verify it automatically.",
        )

    def start_plugin_updates(
        self,
        *,
        selected_keys: list[str],
        actor: str,
    ) -> PluginUpdateBatchOutcome:
        """Queue selected active plugin updates without creating review plans."""
        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        entries = inventory.build_update_workbench(inventory.list_items(limit=200))
        entries_by_key = {entry.plan_key: entry for entry in entries}
        requested_keys = list(dict.fromkeys(key for key in selected_keys if key))

        if not requested_keys:
            raise ValueError("Select at least one active plugin update before starting the run.")
        if any(key not in entries_by_key for key in requested_keys):
            raise ValueError("One or more selected updates are no longer available. Refresh the workbench and try again.")

        selected_entries = [entries_by_key[key] for key in requested_keys]
        for entry in selected_entries:
            scope_error = self._direct_plugin_update_scope_error(entry)
            if scope_error:
                raise ValueError(scope_error)

        selected_site_ids = {entry.site.id for entry in selected_entries}
        active_runs = self.db.scalars(
            select(MaintenanceRun).where(
                MaintenanceRun.kind == self.PLUGIN_UPDATE_KIND,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
                MaintenanceRun.site_id.in_(selected_site_ids),
            )
        )
        if any(active_runs):
            raise ValueError("A direct plugin update is already queued or running for one of the selected sites.")

        batch_id = uuid4().hex
        now = datetime.now(UTC)
        runs: list[MaintenanceRun] = []
        for position, entry in enumerate(selected_entries, start=1):
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
                    "plugin_file": entry.identifier,
                    "plugin_name": entry.name,
                    "current_version": entry.current_version,
                    "target_version": entry.target_version,
                    "stage": "queued",
                    "stage_message": "Queued for direct update after a fresh selected-update preflight.",
                },
            )
            run.steps.extend(
                (
                    MaintenanceRunStep(
                        step_key="preflight",
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting for a fresh selected-update check.",
                        result_json={},
                    ),
                    MaintenanceRunStep(
                        step_key="update-plugin",
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting for the preflight to pass.",
                        result_json={},
                    ),
                    MaintenanceRunStep(
                        step_key="postflight-health",
                        status=MaintenanceRunStepStatus.waiting.value,
                        started_at=now,
                        detail="Waiting for the plugin update to complete.",
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
                action="start-direct-plugin-update-run",
                result="queued",
                detail=(
                    f"Queued direct plugin update run {run.id} in batch {batch_id[:12]} for "
                    f"{run.result_json['plugin_name']} {run.result_json['current_version']} -> "
                    f"{run.result_json['target_version']}."
                ),
                request_id=batch_id,
            )
        self.db.commit()
        return PluginUpdateBatchOutcome(
            batch_id=batch_id,
            run_count=len(runs),
            message=(
                f"Queued {len(runs)} direct plugin update{'s' if len(runs) != 1 else ''}. "
                "Each update will verify the selected version and run a health check afterwards."
            ),
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
                    summary["skipped"] += self._skip_queued_plugin_updates(
                        batch_id,
                        failed_run_id=run.id,
                        message=(
                            f"Skipped because direct update run {run.id} failed. "
                            "The batch stops at the first error."
                        ),
                    )
        return summary

    def _poll_plugin_update(self, run: MaintenanceRun) -> str:
        details = self._plugin_update_details(run)
        site = self.repository.get_site(run.site_id)
        if site is None or site.status != SiteStatus.verified.value:
            self._fail_plugin_update_run(run, "The site is no longer verified for direct updates.")
            return "failed"
        if details is None:
            self._fail_plugin_update_run(run, "The direct update run has an invalid plugin scope.")
            return "failed"

        preflight_step = self._find_step(run, "preflight")
        self._start_plugin_update_step(run, preflight_step, "Refreshing available-update evidence.")
        try:
            SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(run.site_id)
        except SiteMcpProxyError as exc:
            self._fail_plugin_update_run(run, f"Direct update preflight failed: {exc.message}")
            return "failed"

        preflight_error = self._direct_plugin_update_preflight_error(run, details)
        if preflight_error:
            self._fail_plugin_update_run(run, preflight_error)
            return "failed"
        self._complete_plugin_update_step(
            run,
            preflight_step,
            "Fresh selected plugin update checks passed.",
        )

        update_step = self._find_step(run, "update-plugin")
        self._start_plugin_update_step(
            run,
            update_step,
            f"Updating {details['plugin_name']} from {details['current_version']} to {details['target_version']}.",
        )
        try:
            payload = self.proxy.execute_ability(
                run.site_id,
                self.PLUGIN_UPDATE_ABILITY,
                {
                    "plugin_file": details["plugin_file"],
                    "expected_current_version": details["current_version"],
                    "expected_target_version": details["target_version"],
                },
                timeout_seconds=180,
            )
        except SiteMcpProxyError as exc:
            self._fail_plugin_update_run(run, f"{details['plugin_name']} update request failed: {exc.message}")
            return "failed"

        result = self._result_from_payload(payload)
        if (
            result.get("updated") is not True
            or result.get("plugin_file") != details["plugin_file"]
            or result.get("installed_version") != details["target_version"]
            or result.get("active") is not True
        ):
            self._fail_plugin_update_run(
                run,
                f"{details['plugin_name']} did not return the selected installed version and active state.",
            )
            return "failed"
        self._complete_plugin_update_step(
            run,
            update_step,
            f"Bridge verified {details['plugin_name']} {details['target_version']} active.",
            result,
        )

        health_step = self._find_step(run, "postflight-health")
        self._start_plugin_update_step(run, health_step, "Checking the public homepage and WordPress REST API.")
        try:
            health_payload = self.proxy.execute_ability(
                run.site_id,
                self.SITE_HEALTH_ABILITY,
                None,
                timeout_seconds=45,
            )
        except SiteMcpProxyError as exc:
            self._fail_plugin_update_run(run, f"{details['plugin_name']} was updated, but the health check could not run: {exc.message}")
            return "failed"

        health_result = self._result_from_payload(health_payload)
        health_error = self._plugin_update_health_error(health_result)
        health_detail = self._plugin_update_health_detail(health_result)
        if health_error:
            self._fail_plugin_update_run(
                run,
                f"{details['plugin_name']} was updated and verified, but {health_error}. No automatic rollback was performed.",
                health_step=health_step,
            )
            return "failed"

        self._complete_plugin_update_step(run, health_step, health_detail, health_result)
        refresh_note = ""
        try:
            SiteInventoryService(db=self.db, cipher=self.cipher).refresh_site_state(run.site_id)
            SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(run.site_id)
        except SiteMcpProxyError as exc:
            refresh_note = f" The update succeeded, but the follow-up scan failed: {exc.message}"

        completed_at = datetime.now(UTC)
        run.status = MaintenanceRunStatus.succeeded.value
        run.completed_at = completed_at
        run.last_checked_at = completed_at
        run.result_json = {
            **(run.result_json or {}),
            "stage": "completed",
            "stage_message": f"{details['plugin_name']} was updated and verified.{refresh_note}",
            "installed_version": details["target_version"],
        }
        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-hub",
            source="hub-worker",
            action="complete-direct-plugin-update-run",
            result="succeeded",
            detail=(
                f"Direct update run {run.id} updated {details['plugin_name']} "
                f"{details['current_version']} -> {details['target_version']}. {health_detail}{refresh_note}"
            ),
            request_id=self._plugin_update_batch_id(run),
        )
        self.db.commit()
        return "succeeded"

    def _direct_plugin_update_preflight_error(self, run: MaintenanceRun, details: dict[str, str]) -> str | None:
        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        entries = inventory.build_update_workbench(inventory.list_items(limit=200))
        current_entry = next(
            (
                entry
                for entry in entries
                if entry.site.id == run.site_id
                and entry.kind == "plugin"
                and entry.identifier == details["plugin_file"]
            ),
            None,
        )
        if current_entry is None:
            return f"{details['plugin_name']} is no longer listed as an available plugin update."
        if (
            current_entry.current_version != details["current_version"]
            or current_entry.target_version != details["target_version"]
        ):
            return f"{details['plugin_name']} changed since it was selected. Refresh the workbench and start a new run."

        scope_error = self._direct_plugin_update_scope_error(current_entry)
        if scope_error:
            return scope_error

        return None

    @staticmethod
    def _direct_plugin_update_scope_error(entry: UpdateWorkbenchEntry) -> str | None:
        if entry.kind != "plugin":
            return "Direct updates currently support active WordPress plugins only."
        if entry.is_active is not True:
            return f"{entry.name} is inactive. Direct updates currently require an active plugin."
        if not MaintenanceRunService._is_plugin_file(entry.identifier):
            return f"{entry.name} does not have a valid WordPress plugin file."
        if not entry.current_version or not entry.target_version:
            return f"{entry.name} does not report both the installed and target version."
        return None

    @staticmethod
    def _plugin_update_health_error(result: object) -> str | None:
        if not isinstance(result, dict):
            return "the Bridge did not return a verifiable health result"
        if result.get("home_healthy") is not True:
            return f"the public homepage health check did not pass (HTTP {MaintenanceRunService._health_status(result.get('home_status'))})"
        if result.get("rest_healthy") is not True:
            return f"the WordPress REST API health check did not pass (HTTP {MaintenanceRunService._health_status(result.get('rest_status'))})"
        return None

    @staticmethod
    def _plugin_update_health_detail(result: object) -> str:
        if not isinstance(result, dict):
            return "Post-update health check returned no verifiable result."
        return (
            "Post-update health check: "
            f"homepage HTTP {MaintenanceRunService._health_status(result.get('home_status'))}; "
            f"WordPress REST API HTTP {MaintenanceRunService._health_status(result.get('rest_status'))}."
        )

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
        write_audit_log(
            self.db,
            site=run.site,
            actor="kosmos-hub",
            source="hub-worker",
            action="direct-plugin-update-run",
            result="failed",
            detail=f"Direct update run {run.id} failed: {message}",
            request_id=self._plugin_update_batch_id(run),
        )
        self.db.commit()

    def _skip_queued_plugin_updates(self, batch_id: str, *, failed_run_id: int, message: str) -> int:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(
                MaintenanceRun.kind == self.PLUGIN_UPDATE_KIND,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.id.asc())
        )
        skipped = 0
        completed_at = datetime.now(UTC)
        for run in self.db.scalars(statement):
            if run.id == failed_run_id or self._plugin_update_batch_id(run) != batch_id:
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
            write_audit_log(
                self.db,
                site=run.site,
                actor="kosmos-hub",
                source="hub-worker",
                action="direct-plugin-update-run",
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
    def _plugin_update_details(run: MaintenanceRun) -> dict[str, str] | None:
        values = run.result_json or {}
        if not isinstance(values, dict):
            return None
        details = {
            "plugin_file": values.get("plugin_file"),
            "plugin_name": values.get("plugin_name"),
            "current_version": values.get("current_version"),
            "target_version": values.get("target_version"),
        }
        if not all(isinstance(value, str) and value.strip() for value in details.values()):
            return None
        if not MaintenanceRunService._is_plugin_file(details["plugin_file"]):
            return None
        return {key: value.strip() for key, value in details.items()}

    @staticmethod
    def _is_plugin_file(identifier: str | None) -> bool:
        return isinstance(identifier, str) and re.fullmatch(
            r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*\.php",
            identifier,
        ) is not None

    @staticmethod
    def _health_status(value: object) -> str:
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else "not reported"

    def poll_active_updraftplus_backups(self, *, limit: int = 25) -> dict[str, int]:
        statement = (
            select(MaintenanceRun)
            .options(selectinload(MaintenanceRun.steps), selectinload(MaintenanceRun.site))
            .where(
                MaintenanceRun.kind == self.UPDRAFT_BACKUP_KIND,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
            .order_by(MaintenanceRun.started_at.asc())
            .limit(limit)
        )
        runs = list(self.db.scalars(statement))
        summary = {"checked": 0, "succeeded": 0, "failed": 0, "waiting": 0}
        for run in runs:
            summary["checked"] += 1
            outcome = self._poll_updraftplus_backup(run)
            summary[outcome] += 1
        return summary

    def _poll_updraftplus_backup(self, run: MaintenanceRun) -> str:
        now = datetime.now(UTC)
        if not self._is_backup_nonce(run.bridge_backup_nonce):
            self._fail_run(run, actor="kosmos-hub", message="The backup run has no valid UpdraftPlus backup identifier.")
            return "failed"

        if self._backup_was_verified(run):
            return self._poll_updraftplus_backup_cleanup(run)

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
                "bridge_status": "cleanup",
                "bridge_status_message": "The protected backup was verified. The Hub is now checking the oldest eligible backup for cleanup.",
            }
            if verification_step is not None:
                verification_step.status = MaintenanceRunStepStatus.succeeded.value
                verification_step.completed_at = datetime.now(UTC)
                verification_step.detail = "UpdraftPlus recorded the requested complete backup and its protection from automatic deletion."
                verification_step.result_json = dict(run.result_json)
            self.db.commit()
            return self._poll_updraftplus_backup_cleanup(run)

        self._mark_waiting(
            run,
            verification_step,
            self._backup_waiting_detail(bridge_status, bridge_message),
        )
        return "waiting"

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
