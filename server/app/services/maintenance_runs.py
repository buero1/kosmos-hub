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
    START_BACKUP_TIMEOUT_SECONDS = 180
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
        if result.get("accepted") is not True or not self._is_backup_nonce(backup_nonce):
            message = self._safe_message(result.get("message"), "UpdraftPlus did not accept a new backup request.")
            self._fail_run(run, actor=actor, message=message)
            return MaintenanceRunOutcome(run=run, result="failed", message=message)

        run.bridge_backup_nonce = backup_nonce
        run.result_json = {
            "provider": "updraftplus",
            "backup_nonce": backup_nonce,
            "scheduled_at": self._safe_string(result.get("scheduled_at")),
        }
        request_step.status = MaintenanceRunStepStatus.succeeded.value
        request_step.completed_at = datetime.now(UTC)
        request_step.detail = "UpdraftPlus accepted a new full backup request."
        request_step.result_json = dict(run.result_json)
        self.db.add(
            MaintenanceRunStep(
                run=run,
                step_key="verify-backup",
                status=MaintenanceRunStepStatus.waiting.value,
                started_at=datetime.now(UTC),
                detail="Waiting for UpdraftPlus to record the requested complete backup.",
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
            message="A new UpdraftPlus backup was requested. The Hub will verify it automatically.",
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

        if now - self._as_utc(run.started_at) > self.BACKUP_TIMEOUT:
            self._fail_run(
                run,
                actor="kosmos-hub",
                message="UpdraftPlus did not record the requested complete backup within three minutes.",
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
        if result.get("installed") is not True or result.get("active") is not True:
            self._fail_run(run, actor="kosmos-hub", message="UpdraftPlus is no longer installed and active on this site.")
            return "failed"

        if self._is_verified_backup_result(result, run.bridge_backup_nonce):
            snapshot = SiteBackupService(db=self.db, cipher=self.cipher).store_backup_status_result(run.site_id, result)["snapshot"]
            run.status = MaintenanceRunStatus.succeeded.value
            run.completed_at = datetime.now(UTC)
            run.last_checked_at = run.completed_at
            run.result_json = {
                "provider": "updraftplus",
                "backup_nonce": run.bridge_backup_nonce,
                "backup_at": snapshot.backup_at.isoformat() if snapshot.backup_at else None,
                "components": snapshot.components_json,
            }
            if verification_step is not None:
                verification_step.status = MaintenanceRunStepStatus.succeeded.value
                verification_step.completed_at = run.completed_at
                verification_step.detail = "UpdraftPlus recorded the requested complete backup."
                verification_step.result_json = dict(run.result_json)
            write_audit_log(
                self.db,
                site=run.site,
                actor="kosmos-hub",
                source="hub-worker",
                action="verify-updraftplus-backup-run",
                result="succeeded",
                detail=f"Maintenance run {run.id} verified a fresh complete UpdraftPlus backup.",
            )
            self.db.commit()
            return "succeeded"

        self._mark_waiting(
            run,
            verification_step,
            "UpdraftPlus has not yet recorded the requested complete backup. The Hub will check again automatically.",
        )
        return "waiting"

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
        verification_step = self._find_step(run, "verify-backup")
        target_step = verification_step or self._find_step(run, "request-backup")
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
    def _is_backup_nonce(value: object) -> bool:
        return isinstance(value, str) and len(value) == 12 and all(character in "0123456789abcdef" for character in value)

    @classmethod
    def _is_verified_backup_result(cls, result: dict[str, Any], backup_nonce: str | None) -> bool:
        return (
            result.get("available") is True
            and result.get("complete") is True
            and result.get("backup_nonce") == backup_nonce
            and isinstance(result.get("latest_backup_at"), str)
            and bool(result["latest_backup_at"].strip())
        )

    @staticmethod
    def _find_step(run: MaintenanceRun, step_key: str) -> MaintenanceRunStep | None:
        return next((step for step in run.steps if step.step_key == step_key), None)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
