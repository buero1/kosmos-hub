from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.site import SiteStatus
from app.models.site_backup_snapshot import SiteBackupSnapshot
from app.models.update_plan import UpdatePlan, UpdatePlanItem
from app.repositories.site_repository import SiteRepository
from app.repositories.update_plan_repository import UpdatePlanRepository
from app.services.audit import write_audit_log
from app.services.fleet_inventory import FleetInventoryService, UpdateWorkbenchEntry
from app.services.site_backups import SiteBackupService
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService
from app.services.site_updates import SiteUpdateService


@dataclass(frozen=True)
class UpdatePlanPreflightItem:
    item: UpdatePlanItem
    site_verified: bool
    update_still_available: bool
    backup_status: str
    backup_provider: str | None
    backup_at: datetime | None

    @property
    def execution_ready(self) -> bool:
        return self.site_verified and self.update_still_available and self.backup_status == "available"

    @property
    def status_label(self) -> str:
        return "Ready" if self.execution_ready else "Blocked"

    @property
    def next_step(self) -> str:
        if not self.site_verified:
            return "Site is not verified"
        if not self.update_still_available:
            return "Refresh the update inventory before review"
        if self.backup_status == "not checked":
            return "Refresh the backup status before review"
        if self.backup_status == "provider not installed":
            return "Install and configure a backup provider"
        if self.backup_status == "provider not active":
            return "Activate and verify the backup provider"
        if self.backup_status == "no complete backup":
            return "Create and verify a complete backup"
        if self.backup_status == "backup stale":
            return "Create and verify a fresh backup"
        return "Backup is ready. A scoped update can now be approved when enabled."


@dataclass(frozen=True)
class UpdatePlanExecutionResult:
    result: str
    message: str


class UpdatePlanService:
    MAINWP_CHILD_PLUGIN = "mainwp-child/mainwp-child.php"
    MAINWP_CHILD_ABILITY = "kosmos-bridge/update-mainwp-child"
    MAINWP_CHILD_ACTIVATION_ABILITY = "kosmos-bridge/activate-mainwp-child"

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = UpdatePlanRepository(db)

    def create_draft(
        self,
        *,
        name: str,
        notes: str,
        selected_keys: list[str],
        created_by: str,
    ) -> UpdatePlan:
        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        entries = inventory.build_update_workbench(inventory.list_items(limit=200))
        entries_by_key = {entry.plan_key: entry for entry in entries}
        requested_keys = list(dict.fromkeys(key for key in selected_keys if key))

        if not requested_keys:
            raise ValueError("Select at least one current update before creating a plan.")
        if any(key not in entries_by_key for key in requested_keys):
            raise ValueError("One or more selected updates are no longer available. Refresh the workbench and try again.")

        selected_entries = [entries_by_key[key] for key in requested_keys]
        normalized_name = name.strip() or f"Update review for {len(selected_entries)} items"
        normalized_notes = notes.strip() or None
        plan = self.repository.create(name=normalized_name, created_by=created_by, notes=normalized_notes)

        for entry in selected_entries:
            item = self._add_plan_item(plan, entry)
            write_audit_log(
                self.db,
                site=entry.site,
                actor=created_by,
                source="hub",
                action="create-update-plan",
                result="draft",
                detail=f"Added {item.update_type} update {item.update_name} to plan {plan.id}.",
            )

        self.db.commit()
        self.db.refresh(plan)
        return plan

    def list_plans(self) -> list[UpdatePlan]:
        return self.repository.list()

    def get_plan(self, plan_id: int) -> UpdatePlan | None:
        return self.repository.get(plan_id)

    def mainwp_child_scope_error(self, plan: UpdatePlan) -> str | None:
        if len(plan.items) != 1:
            return "This execution path only accepts a plan with exactly one update."

        item = plan.items[0]
        if item.update_type != "plugin" or item.update_identifier != self.MAINWP_CHILD_PLUGIN:
            return "This execution path is restricted to the MainWP Child plugin."
        if item.is_active is not True:
            return "MainWP Child must be active before it can be updated by the Hub."
        if not item.current_version or not item.target_version:
            return "The MainWP Child update must include both current and target versions."
        return None

    def mainwp_child_recovery_scope_error(self, plan: UpdatePlan) -> str | None:
        if len(plan.items) != 1:
            return "This recovery path only accepts a plan with exactly one update."

        item = plan.items[0]
        if item.update_type != "plugin" or item.update_identifier != self.MAINWP_CHILD_PLUGIN:
            return "This recovery path is restricted to the MainWP Child plugin."
        if not item.target_version:
            return "The MainWP Child recovery requires the approved installed version."
        return None

    def approve_mainwp_child(self, *, plan_id: int, actor: str) -> UpdatePlanExecutionResult:
        plan = self._require_plan(plan_id)
        if plan.status != "draft":
            return UpdatePlanExecutionResult("blocked", "Only a draft plan can be approved.")

        scope_error = self.mainwp_child_scope_error(plan)
        if scope_error:
            return UpdatePlanExecutionResult("blocked", scope_error)

        try:
            self._refresh_preflight_evidence(plan.items[0].site_id)
        except SiteMcpProxyError as exc:
            return self._block_plan(plan, actor, f"Approval blocked because the current preflight could not be refreshed: {exc.message}")

        plan = self._require_plan(plan_id)
        readiness_error = self._mainwp_child_readiness_error(plan)
        if readiness_error:
            return self._block_plan(plan, actor, readiness_error)

        plan.status = "approved"
        self._write_plan_audit(
            plan,
            actor=actor,
            action="approve-mainwp-child-update",
            result="approved",
            detail=(
                f"Approved MainWP Child {plan.items[0].current_version} -> "
                f"{plan.items[0].target_version} after a fresh backup and update preflight."
            ),
        )
        self.db.commit()
        return UpdatePlanExecutionResult("approved", "MainWP Child was approved. No update has been run yet.")

    def execute_mainwp_child(self, *, plan_id: int, actor: str) -> UpdatePlanExecutionResult:
        plan = self._require_plan(plan_id)
        if plan.status != "approved":
            return UpdatePlanExecutionResult("blocked", "Only an approved MainWP Child plan can be executed.")

        scope_error = self.mainwp_child_scope_error(plan)
        if scope_error:
            return self._block_plan(plan, actor, scope_error)

        try:
            self._refresh_preflight_evidence(plan.items[0].site_id)
        except SiteMcpProxyError as exc:
            return self._block_plan(plan, actor, f"Execution blocked because the current preflight could not be refreshed: {exc.message}")

        plan = self._require_plan(plan_id)
        readiness_error = self._mainwp_child_readiness_error(plan)
        if readiness_error:
            return self._block_plan(plan, actor, readiness_error)

        item = plan.items[0]
        try:
            payload = SiteMcpProxyService(db=self.db, cipher=self.cipher).execute_ability(
                item.site_id,
                self.MAINWP_CHILD_ABILITY,
                {
                    "expected_current_version": item.current_version,
                    "expected_target_version": item.target_version,
                },
                timeout_seconds=180,
            )
        except SiteMcpProxyError as exc:
            return self._fail_plan(plan, actor, f"MainWP Child update request failed: {exc.message}")

        result = payload.get("result")
        if not isinstance(result, dict):
            return self._fail_plan(plan, actor, "MainWP Child returned no verifiable update result.")

        if (
            result.get("updated") is True
            and result.get("plugin_file") == self.MAINWP_CHILD_PLUGIN
            and result.get("installed_version") == item.target_version
            and result.get("active") is not True
        ):
            return self._fail_plan(
                plan,
                actor,
                "MainWP Child was updated to the approved version but is inactive. Use the explicit recovery action to reactivate that verified version.",
            )

        if (
            result.get("updated") is not True
            or result.get("plugin_file") != self.MAINWP_CHILD_PLUGIN
            or result.get("installed_version") != item.target_version
            or result.get("active") is not True
        ):
            return self._fail_plan(plan, actor, "MainWP Child did not return the approved installed version and active state.")

        refresh_note = ""
        try:
            SiteInventoryService(db=self.db, cipher=self.cipher).refresh_site_state(item.site_id)
            SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(item.site_id)
        except SiteMcpProxyError as exc:
            refresh_note = f" The update succeeded, but the follow-up scan failed: {exc.message}"

        plan.status = "executed"
        self._write_plan_audit(
            plan,
            actor=actor,
            action="execute-mainwp-child-update",
            result="executed",
            detail=(
                f"Updated MainWP Child {item.current_version} -> {item.target_version}. "
                f"Bridge verified installed version {result.get('installed_version')}.{refresh_note}"
            ),
        )
        self.db.commit()
        return UpdatePlanExecutionResult("executed", f"MainWP Child was updated and verified.{refresh_note}")

    def recover_mainwp_child_activation(self, *, plan_id: int, actor: str) -> UpdatePlanExecutionResult:
        plan = self._require_plan(plan_id)
        if plan.status != "failed":
            return UpdatePlanExecutionResult("blocked", "Only a failed MainWP Child plan can use this recovery action.")

        scope_error = self.mainwp_child_recovery_scope_error(plan)
        if scope_error:
            return UpdatePlanExecutionResult("blocked", scope_error)

        item = plan.items[0]
        try:
            payload = SiteMcpProxyService(db=self.db, cipher=self.cipher).execute_ability(
                item.site_id,
                self.MAINWP_CHILD_ACTIVATION_ABILITY,
                {"expected_installed_version": item.target_version},
                timeout_seconds=60,
            )
        except SiteMcpProxyError as exc:
            return self._fail_plan(
                plan,
                actor,
                f"MainWP Child recovery request failed: {exc.message}",
                action="recover-mainwp-child-activation",
            )

        result = payload.get("result")
        if (
            not isinstance(result, dict)
            or result.get("plugin_file") != self.MAINWP_CHILD_PLUGIN
            or result.get("installed_version") != item.target_version
            or result.get("active") is not True
        ):
            return self._fail_plan(
                plan,
                actor,
                "MainWP Child activation could not be verified for the approved installed version.",
                action="recover-mainwp-child-activation",
            )

        refresh_note = ""
        try:
            SiteInventoryService(db=self.db, cipher=self.cipher).refresh_site_state(item.site_id)
            SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(item.site_id)
        except SiteMcpProxyError as exc:
            refresh_note = f" The reactivation succeeded, but the follow-up scan failed: {exc.message}"

        plan.status = "executed"
        self._write_plan_audit(
            plan,
            actor=actor,
            action="recover-mainwp-child-activation",
            result="executed",
            detail=(
                f"Reactivated MainWP Child at the verified installed version {item.target_version}."
                f"{refresh_note}"
            ),
        )
        self.db.commit()
        return UpdatePlanExecutionResult("executed", f"MainWP Child was reactivated and verified.{refresh_note}")

    def _require_plan(self, plan_id: int) -> UpdatePlan:
        plan = self.repository.get(plan_id)
        if plan is None:
            raise ValueError("Update plan was not found.")
        return plan

    def _refresh_preflight_evidence(self, site_id: int) -> None:
        SiteBackupService(db=self.db, cipher=self.cipher).refresh_site_backup_status(site_id)
        SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(site_id)

    def _mainwp_child_readiness_error(self, plan: UpdatePlan) -> str | None:
        scope_error = self.mainwp_child_scope_error(plan)
        if scope_error:
            return scope_error

        preflight = self.build_preflight(plan)
        if len(preflight) != 1 or not preflight[0].execution_ready:
            if not preflight:
                return "The MainWP Child preflight could not be created."
            return f"Execution remains blocked: {preflight[0].next_step}."
        return None

    def _block_plan(self, plan: UpdatePlan, actor: str, detail: str) -> UpdatePlanExecutionResult:
        plan.status = "blocked"
        self._write_plan_audit(plan, actor=actor, action="block-mainwp-child-update", result="blocked", detail=detail)
        self.db.commit()
        return UpdatePlanExecutionResult("blocked", detail)

    def _fail_plan(
        self,
        plan: UpdatePlan,
        actor: str,
        detail: str,
        *,
        action: str = "execute-mainwp-child-update",
    ) -> UpdatePlanExecutionResult:
        plan.status = "failed"
        self._write_plan_audit(plan, actor=actor, action=action, result="failed", detail=detail)
        self.db.commit()
        return UpdatePlanExecutionResult("failed", detail)

    def _write_plan_audit(
        self,
        plan: UpdatePlan,
        *,
        actor: str,
        action: str,
        result: str,
        detail: str,
    ) -> None:
        write_audit_log(
            self.db,
            site=plan.items[0].site if plan.items else None,
            actor=actor,
            source="hub-web",
            action=action,
            result=result,
            detail=f"Plan {plan.id}: {detail}",
        )

    def build_preflight(self, plan: UpdatePlan) -> list[UpdatePlanPreflightItem]:
        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        entries = inventory.build_update_workbench(inventory.list_items(limit=200))
        current_entries = {
            (entry.site.id, entry.kind, entry.identifier): entry
            for entry in entries
        }
        backup_snapshots = SiteRepository(self.db).get_latest_backup_snapshots_by_site_ids(
            list({item.site_id for item in plan.items})
        )
        checks = []
        for item in plan.items:
            backup_status, backup_provider, backup_at = self._backup_preflight(
                backup_snapshots.get(item.site_id)
            )
            checks.append(
                UpdatePlanPreflightItem(
                    item=item,
                    site_verified=item.site.status == SiteStatus.verified.value,
                    update_still_available=self._matches_current_entry(current_entries, item),
                    backup_status=backup_status,
                    backup_provider=backup_provider,
                    backup_at=backup_at,
                )
            )
        return checks

    def _add_plan_item(self, plan: UpdatePlan, entry: UpdateWorkbenchEntry) -> UpdatePlanItem:
        item = UpdatePlanItem(
            site=entry.site,
            update_type=entry.kind,
            update_identifier=entry.identifier or entry.name,
            update_name=entry.name,
            current_version=entry.current_version or None,
            target_version=entry.target_version or None,
            is_active=entry.is_active,
            snapshot_captured_at=entry.captured_at,
        )
        plan.items.append(item)
        return item

    def _matches_current_entry(
        self,
        current_entries: dict[tuple[int, str, str], UpdateWorkbenchEntry],
        item: UpdatePlanItem,
    ) -> bool:
        entry = current_entries.get((item.site_id, item.update_type, item.update_identifier))
        if entry is None:
            return False
        return entry.current_version == (item.current_version or "") and entry.target_version == (item.target_version or "")

    @staticmethod
    def _backup_preflight(snapshot: SiteBackupSnapshot | None) -> tuple[str, str | None, datetime | None]:
        if snapshot is None:
            return "not checked", None, None

        backup_at = UpdatePlanService._as_utc(snapshot.backup_at)
        if not snapshot.provider_installed:
            return "provider not installed", snapshot.provider, backup_at
        if not snapshot.provider_active:
            return "provider not active", snapshot.provider, backup_at
        if not snapshot.backup_complete or not snapshot.backup_available or backup_at is None:
            return "no complete backup", snapshot.provider, backup_at
        if backup_at < datetime.now(UTC) - timedelta(days=7):
            return "backup stale", snapshot.provider, backup_at
        return "available", snapshot.provider, backup_at

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
