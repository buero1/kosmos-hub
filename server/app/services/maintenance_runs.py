from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

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
from app.services.site_backups import SiteBackupService
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


@dataclass(frozen=True)
class MaintenanceRunOutcome:
    run: MaintenanceRun
    result: str
    message: str


class MaintenanceRunService:
    """Run bounded maintenance tasks and persist evidence for later automation."""

    UPDRAFT_BACKUP_KIND = "updraftplus-backup"
    START_BACKUP_ABILITY = "kosmos-bridge/start-updraftplus-backup"
    BACKUP_STATUS_ABILITY = "kosmos-bridge/get-updraftplus-backup-status"
    LIST_BACKUPS_ABILITY = "kosmos-bridge/list-updraftplus-backups"
    DELETE_BACKUP_ABILITY = "kosmos-bridge/delete-updraftplus-backup"
    VERIFY_BACKUP_DELETION_ABILITY = "kosmos-bridge/verify-updraftplus-backup-deletion"
    START_BACKUP_TIMEOUT_SECONDS = 20
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
                "continue_delete": False,
                "processed_instance_ids": [],
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
                    "continue_delete": cleanup.get("continue_delete") is True,
                    "processed_instance_ids": cleanup.get("processed_instance_ids", []),
                },
                timeout_seconds=30,
            )
        except SiteMcpProxyError as exc:
            self._fail_run(run, actor="kosmos-hub", message=f"Backup cleanup could not start or continue: {exc.message}")
            return "failed"

        result = self._result_from_payload(payload)
        cleanup = {
            **cleanup,
            "status": self._safe_string(result.get("status")) or "failed",
            "continue_delete": True,
            "processed_instance_ids": result.get("processed_instance_ids") if isinstance(result.get("processed_instance_ids"), list) else [],
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
                timeout_seconds=30,
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
