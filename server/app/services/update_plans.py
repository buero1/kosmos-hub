from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.site import SiteStatus
from app.models.update_plan import UpdatePlan, UpdatePlanItem
from app.repositories.update_plan_repository import UpdatePlanRepository
from app.services.audit import write_audit_log
from app.services.fleet_inventory import FleetInventoryService, UpdateWorkbenchEntry


@dataclass(frozen=True)
class UpdatePlanPreflightItem:
    item: UpdatePlanItem
    site_verified: bool
    update_still_available: bool
    backup_status: str

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
        return "Connect and verify a backup provider before execution"


class UpdatePlanService:
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

    def build_preflight(self, plan: UpdatePlan) -> list[UpdatePlanPreflightItem]:
        inventory = FleetInventoryService(db=self.db, cipher=self.cipher)
        entries = inventory.build_update_workbench(inventory.list_items(limit=200))
        current_entries = {
            (entry.site.id, entry.kind, entry.identifier): entry
            for entry in entries
        }
        return [
            UpdatePlanPreflightItem(
                item=item,
                site_verified=item.site.status == SiteStatus.verified.value,
                update_still_available=self._matches_current_entry(current_entries, item),
                backup_status="not configured",
            )
            for item in plan.items
        ]

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
