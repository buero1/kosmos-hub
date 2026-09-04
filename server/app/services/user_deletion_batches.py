from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import SecretCipher
from app.models.user_deletion_batch import UserDeletionBatch, UserDeletionBatchItem
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_users import SiteUserService, UserWorkbenchEntry


class UserDeletionBatchService:
    """Prepare, execute, and resume reviewed WordPress user deletions safely."""

    TEXT_CONFIRMATION_THRESHOLD = SiteUserService.BULK_ACTION_LIMIT
    TEXT_CONFIRMATION_VALUE = "delete"

    BATCH_PREPARED = "prepared"
    BATCH_QUEUED = "queued"
    BATCH_RUNNING = "running"
    BATCH_COMPLETED = "completed"
    BATCH_CANCELLED = "cancelled"

    ITEM_READY = "ready"
    ITEM_BLOCKED = "blocked"
    ITEM_QUEUED = "queued"
    ITEM_RUNNING = "running"
    ITEM_SUCCEEDED = "succeeded"
    ITEM_FAILED = "failed"
    ITEM_SKIPPED = "skipped"
    ITEM_CANCELLED = "cancelled"

    TERMINAL_ITEM_STATUSES = frozenset({ITEM_BLOCKED, ITEM_SUCCEEDED, ITEM_FAILED, ITEM_SKIPPED, ITEM_CANCELLED})

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def prepare_batch(
        self,
        *,
        selected_keys: list[str],
        actor: str,
        deletion_confirmation: str = "",
    ) -> UserDeletionBatch:
        user_service = self._user_service()
        targets = user_service.selected_workbench_entries(selected_keys, limit=None)
        if (
            len(targets) > self.TEXT_CONFIRMATION_THRESHOLD
            and deletion_confirmation.strip().casefold() != self.TEXT_CONFIRMATION_VALUE
        ):
            raise ValueError(
                f'Type "{self.TEXT_CONFIRMATION_VALUE}" to prepare deletion of {len(targets)} WordPress users.'
            )
        entries_by_site = self._entries_by_site(user_service.list_workbench_entries())
        selected_ids_by_site = self._selected_ids_by_site_from_entries(targets)
        batch = UserDeletionBatch(
            requested_by=actor,
            status=self.BATCH_PREPARED,
            cancellation_requested=False,
        )
        self.db.add(batch)
        self.db.flush()

        for position, target in enumerate(targets, start=1):
            candidates = self._replacement_candidates(
                entries_by_site.get(target.site.id, []),
                selected_ids_by_site.get(target.site.id, set()),
            )
            item = UserDeletionBatchItem(
                user_deletion_batch_id=batch.id,
                site_id=target.site.id,
                position=position,
                target_user_id=self._user_id(target.user),
                target_username=str(target.user["username"]),
                replacement_user_id=None,
                replacement_username=None,
                status=self.ITEM_READY,
                message=None,
            )
            if not target.supports_delete:
                item.status = self.ITEM_BLOCKED
                item.message = "The connected Bridge does not support deleting WordPress users on this website."
            elif not candidates:
                item.status = self.ITEM_BLOCKED
                item.message = "No other stored administrator is available on this website. Refresh users or retain another administrator first."
            else:
                default_replacement = candidates[0]
                item.replacement_user_id = self._user_id(default_replacement.user)
                item.replacement_username = str(default_replacement.user["username"])
                item.message = f"Content will be reassigned to administrator {item.replacement_username}."
            batch.items.append(item)
        self.db.flush()
        return batch

    def get_batch(self, batch_id: int) -> UserDeletionBatch | None:
        statement = (
            select(UserDeletionBatch)
            .options(selectinload(UserDeletionBatch.items).selectinload(UserDeletionBatchItem.site))
            .where(UserDeletionBatch.id == batch_id)
        )
        return self.db.scalar(statement)

    def preparation_rows(self, batch: UserDeletionBatch) -> list[dict[str, Any]]:
        entries_by_site = self._entries_by_site(self._user_service().list_workbench_entries())
        selected_ids_by_site = self._selected_ids_by_site_from_items(batch.items)
        rows: list[dict[str, Any]] = []
        for item in batch.items:
            candidates = self._replacement_candidates(
                entries_by_site.get(item.site_id, []),
                selected_ids_by_site.get(item.site_id, set()),
            )
            rows.append({"item": item, "candidates": candidates})
        return rows

    def batch_can_start(self, batch: UserDeletionBatch, rows: list[dict[str, Any]]) -> bool:
        if batch.status != self.BATCH_PREPARED:
            return False
        for row in rows:
            item = row["item"]
            candidate_ids = {self._user_id(candidate.user) for candidate in row["candidates"]}
            if item.status != self.ITEM_READY or item.replacement_user_id not in candidate_ids:
                return False
        return bool(rows)

    def start_batch(
        self,
        *,
        batch_id: int,
        item_ids: list[int],
        replacement_user_ids: list[int],
    ) -> UserDeletionBatch:
        batch = self._required_batch(batch_id)
        if batch.status != self.BATCH_PREPARED:
            raise ValueError("This deletion batch has already been started or is no longer available.")
        if len(item_ids) != len(replacement_user_ids):
            raise ValueError("Choose an administrator for every selected WordPress user.")
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("Every selected WordPress user must appear only once in the deletion review.")

        items_by_id = {item.id: item for item in batch.items}
        if set(item_ids) != set(items_by_id):
            raise ValueError("The deletion review is out of date. Prepare the selected users again.")

        rows_by_item_id = {row["item"].id: row for row in self.preparation_rows(batch)}
        replacements_by_item_id = dict(zip(item_ids, replacement_user_ids, strict=True))
        for item_id, item in items_by_id.items():
            row = rows_by_item_id[item_id]
            candidates = row["candidates"]
            candidate_by_id = {self._user_id(candidate.user): candidate for candidate in candidates}
            replacement_id = replacements_by_item_id[item_id]
            replacement = candidate_by_id.get(replacement_id)
            if item.status != self.ITEM_READY or replacement is None:
                raise ValueError(
                    f"No valid other administrator is currently available for {item.target_username} on {item.site.domain}."
                )
            item.replacement_user_id = replacement_id
            item.replacement_username = str(replacement.user["username"])
            item.status = self.ITEM_QUEUED
            item.message = "Queued for safe deletion."

        batch.status = self.BATCH_QUEUED
        batch.cancellation_requested = False
        batch.started_at = None
        batch.completed_at = None
        self.db.flush()
        return batch

    def cancel_batch(self, *, batch_id: int) -> UserDeletionBatch:
        batch = self._required_batch(batch_id)
        if batch.status not in {self.BATCH_PREPARED, self.BATCH_QUEUED, self.BATCH_RUNNING}:
            raise ValueError("Only a prepared, queued, or running deletion batch can be cancelled.")

        now = self._now()
        batch.cancellation_requested = True
        for item in batch.items:
            if item.status in {self.ITEM_READY, self.ITEM_QUEUED}:
                item.status = self.ITEM_CANCELLED
                item.message = "Cancelled before deletion started."
                item.completed_at = now
        self._finalize_batch(batch, now=now)
        self.db.flush()
        return batch

    def recover_interrupted_batches(self) -> int:
        """Return claimed rows to the queue after an application restart."""
        statement = (
            select(UserDeletionBatch)
            .options(selectinload(UserDeletionBatch.items))
            .where(UserDeletionBatch.status.in_((self.BATCH_QUEUED, self.BATCH_RUNNING)))
        )
        recovered = 0
        now = self._now()
        for batch in self.db.scalars(statement):
            for item in batch.items:
                if item.status != self.ITEM_RUNNING:
                    continue
                if batch.cancellation_requested:
                    item.status = self.ITEM_CANCELLED
                    item.message = "Cancelled after application restart before deletion continued."
                    item.completed_at = now
                else:
                    item.status = self.ITEM_QUEUED
                    item.message = "Recovered after application restart and queued again."
                    item.started_at = None
                recovered += 1
            self._finalize_batch(batch, now=now)
        if recovered:
            self.db.commit()
        return recovered

    def claim_next_item_id(self) -> int | None:
        """Claim one queued deletion so a second worker cannot process it too."""
        statement = (
            select(UserDeletionBatchItem)
            .join(UserDeletionBatch)
            .options(selectinload(UserDeletionBatchItem.batch))
            .where(
                UserDeletionBatchItem.status == self.ITEM_QUEUED,
                UserDeletionBatch.status.in_((self.BATCH_QUEUED, self.BATCH_RUNNING)),
            )
            .order_by(UserDeletionBatch.id.asc(), UserDeletionBatchItem.position.asc())
            .limit(1)
        )
        item = self.db.scalar(statement)
        if item is None:
            return None

        now = self._now()
        if item.batch.cancellation_requested:
            item.status = self.ITEM_CANCELLED
            item.message = "Cancelled before deletion started."
            item.completed_at = now
            self._finalize_batch(item.batch, now=now)
            self.db.commit()
            return None

        item.status = self.ITEM_RUNNING
        item.message = "Refreshing current WordPress users before deletion."
        item.started_at = now
        item.batch.status = self.BATCH_RUNNING
        item.batch.started_at = item.batch.started_at or now
        self.db.commit()
        return item.id

    def process_claimed_item(self, item_id: int) -> str:
        item = self._required_item(item_id)
        if item.status != self.ITEM_RUNNING:
            return item.status

        batch = item.batch
        now = self._now()
        if batch.cancellation_requested:
            item.status = self.ITEM_CANCELLED
            item.message = "Cancelled before deletion started."
            item.completed_at = now
            self._finalize_batch(batch, now=now)
            self.db.commit()
            return item.status

        users = self._user_service()
        try:
            inventory = users.refresh_site_users(item.site_id, actor=batch.requested_by)
            target = self._find_user(inventory.users, item.target_user_id)
            replacement = self._find_user(inventory.users, item.replacement_user_id)
            selected_ids = self._selected_ids_by_site_from_items(batch.items).get(item.site_id, set())
            if target is None:
                self._complete_item(item, self.ITEM_SKIPPED, "Skipped: the selected user no longer exists in WordPress.")
            elif replacement is None:
                self._complete_item(item, self.ITEM_SKIPPED, "Skipped: the chosen reassignment administrator no longer exists in WordPress.")
            elif "administrator" not in replacement.get("roles", []):
                self._complete_item(item, self.ITEM_SKIPPED, "Skipped: the chosen reassignment user is no longer an administrator.")
            elif self._user_id(replacement) in selected_ids:
                self._complete_item(item, self.ITEM_SKIPPED, "Skipped: reassignment to another selected user is not allowed.")
            elif batch.cancellation_requested:
                self._complete_item(item, self.ITEM_CANCELLED, "Cancelled before the WordPress deletion started.")
            else:
                item.target_username = str(target["username"])
                item.replacement_username = str(replacement["username"])
                users.delete_user(
                    site_id=item.site_id,
                    user_id=item.target_user_id,
                    reassign_to_user_id=self._user_id(replacement),
                    confirmed_username=item.target_username,
                    actor=batch.requested_by,
                    refresh_inventory=False,
                )
                try:
                    users.refresh_site_users(item.site_id, actor=batch.requested_by)
                except SiteMcpProxyError as exc:
                    self._complete_item(
                        item,
                        self.ITEM_SUCCEEDED,
                        (
                            f"Deleted by WordPress and content reassigned to administrator {item.replacement_username}. "
                            f"The stored user inventory could not refresh: {exc.message}"
                        ),
                    )
                else:
                    self._complete_item(
                        item,
                        self.ITEM_SUCCEEDED,
                        f"Deleted by WordPress. Content was reassigned to administrator {item.replacement_username}.",
                    )
        except (SiteMcpProxyError, ValueError) as exc:
            self._complete_item(item, self.ITEM_FAILED, str(exc))
        except Exception:
            self._complete_item(item, self.ITEM_FAILED, "The deletion worker stopped unexpectedly. This user was not marked as deleted.")

        self._finalize_batch(batch, now=self._now())
        self.db.commit()
        return item.status

    def status_payload(self, batch: UserDeletionBatch) -> dict[str, Any]:
        completed = sum(item.status in self.TERMINAL_ITEM_STATUSES for item in batch.items)
        return {
            "batch_id": batch.id,
            "status": batch.status,
            "cancellation_requested": batch.cancellation_requested,
            "total": len(batch.items),
            "completed": completed,
            "can_cancel": batch.status in {self.BATCH_QUEUED, self.BATCH_RUNNING} and not batch.cancellation_requested,
            "items": [
                {
                    "id": item.id,
                    "site": item.site.domain,
                    "site_id": item.site_id,
                    "username": item.target_username,
                    "replacement_username": item.replacement_username or "-",
                    "status": item.status,
                    "message": item.message or "",
                }
                for item in batch.items
            ],
        }

    def _required_batch(self, batch_id: int) -> UserDeletionBatch:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise ValueError("The deletion batch no longer exists.")
        return batch

    def _required_item(self, item_id: int) -> UserDeletionBatchItem:
        statement = (
            select(UserDeletionBatchItem)
            .options(selectinload(UserDeletionBatchItem.batch).selectinload(UserDeletionBatch.items))
            .where(UserDeletionBatchItem.id == item_id)
        )
        item = self.db.scalar(statement)
        if item is None:
            raise ValueError("The deletion batch item no longer exists.")
        return item

    def _user_service(self) -> SiteUserService:
        return SiteUserService(db=self.db, cipher=self.cipher)

    @staticmethod
    def _entries_by_site(entries: list[UserWorkbenchEntry]) -> dict[int, list[UserWorkbenchEntry]]:
        result: dict[int, list[UserWorkbenchEntry]] = {}
        for entry in entries:
            result.setdefault(entry.site.id, []).append(entry)
        return result

    @staticmethod
    def _selected_ids_by_site_from_entries(entries: list[UserWorkbenchEntry]) -> dict[int, set[int]]:
        result: dict[int, set[int]] = {}
        for entry in entries:
            result.setdefault(entry.site.id, set()).add(UserDeletionBatchService._user_id(entry.user))
        return result

    @staticmethod
    def _selected_ids_by_site_from_items(items: list[UserDeletionBatchItem]) -> dict[int, set[int]]:
        result: dict[int, set[int]] = {}
        for item in items:
            result.setdefault(item.site_id, set()).add(item.target_user_id)
        return result

    @staticmethod
    def _replacement_candidates(
        entries: list[UserWorkbenchEntry],
        selected_user_ids: set[int],
    ) -> list[UserWorkbenchEntry]:
        return [
            entry
            for entry in entries
            if UserDeletionBatchService._user_id(entry.user) not in selected_user_ids
            and "administrator" in entry.user.get("roles", [])
        ]

    @staticmethod
    def _find_user(users: list[dict[str, Any]], user_id: int | None) -> dict[str, Any] | None:
        if user_id is None:
            return None
        return next((user for user in users if UserDeletionBatchService._user_id(user) == user_id), None)

    @staticmethod
    def _user_id(user: dict[str, Any]) -> int:
        try:
            user_id = int(user["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("The stored WordPress user inventory contains an invalid user ID.") from exc
        if user_id < 1:
            raise ValueError("The stored WordPress user inventory contains an invalid user ID.")
        return user_id

    def _complete_item(self, item: UserDeletionBatchItem, status: str, message: str) -> None:
        item.status = status
        item.message = message
        item.completed_at = self._now()

    def _finalize_batch(self, batch: UserDeletionBatch, *, now: datetime) -> None:
        if any(item.status not in self.TERMINAL_ITEM_STATUSES for item in batch.items):
            return
        batch.status = self.BATCH_CANCELLED if batch.cancellation_requested else self.BATCH_COMPLETED
        batch.completed_at = now

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
