"""Periodic incremental IMAP synchronization for configured Mittwald mailboxes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_imap_import import HubMailboxImapImportItem
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.services.hub_mailbox_imap_import import HubMailboxImapImportService, HubMailboxImapImportError


_FOLDERS = ("INBOX", "INBOX.Sent")
_INITIAL_SYNC_DATE = date(2026, 9, 5)


@dataclass(frozen=True)
class HubMailboxImapSyncSummary:
    checked: int
    imported: int
    skipped: int
    failed: int


class HubMailboxImapSyncService:
    """Fetch messages newer than each folder's durable high-water UID."""

    def __init__(self, *, db: Session, cipher: SecretCipher, public_base_url: str) -> None:
        self.db = db
        self.importer = HubMailboxImapImportService(
            db=db,
            cipher=cipher,
            public_base_url=public_base_url,
        )

    def sync_once(
        self,
        *,
        limit: int = 100,
        folders: tuple[str, ...] = _FOLDERS,
        mailbox_account_ids: tuple[int, ...] | None = None,
    ) -> HubMailboxImapSyncSummary:
        if limit < 1:
            return HubMailboxImapSyncSummary(checked=0, imported=0, skipped=0, failed=0)
        selected_folders = tuple(folder for folder in folders if folder in _FOLDERS)
        if not selected_folders:
            return HubMailboxImapSyncSummary(checked=0, imported=0, skipped=0, failed=0)
        summary = {"checked": 0, "imported": 0, "skipped": 0, "failed": 0}
        statement = (
            select(HubMailboxAccount)
            .where(HubMailboxAccount.enabled.is_(True), HubMailboxAccount.verified_at.is_not(None))
            .order_by(HubMailboxAccount.id.asc())
        )
        if mailbox_account_ids:
            statement = statement.where(HubMailboxAccount.id.in_(mailbox_account_ids))
        accounts = self.db.scalars(statement).all()
        for account in accounts:
            for folder in selected_folders:
                if summary["checked"] >= limit:
                    return HubMailboxImapSyncSummary(**summary)
                state = self._state(account=account, folder=folder)
                try:
                    uids, initial_high_water = self._new_uids(account=account, folder=folder, state=state)
                except HubMailboxImapImportError as exc:
                    self._record_folder_error(state=state, error=str(exc))
                    summary["failed"] += 1
                    continue

                batch_uids = uids[: limit - summary["checked"]]
                for uid in batch_uids:
                    try:
                        raw_message, flags = self.importer._fetch_message(account=account, folder=folder, uid=uid)
                        parsed = self.importer._parse_message(
                            raw_message=raw_message,
                            account=account,
                            folder=folder,
                            uid=uid,
                            flags=flags,
                        )
                        outcome, _attachment_count, _attachment_bytes = self.importer._store_message(parsed=parsed)
                        state.last_imap_uid = self._later_uid(state.last_imap_uid, uid)
                        self._record_folder_success(state=state)
                        self.db.commit()
                    except (HubMailboxImapImportError, ValueError) as exc:
                        self.db.rollback()
                        self._record_folder_error(state=state, error=str(exc))
                        summary["failed"] += 1
                        break
                    except Exception:
                        self.db.rollback()
                        self._record_folder_error(
                            state=state,
                            error="Eine neue Mittwald-E-Mail konnte nicht sicher übernommen werden.",
                        )
                        summary["failed"] += 1
                        break
                    summary["checked"] += 1
                    summary["imported" if outcome == "imported" else "skipped"] += 1
                else:
                    # An initial empty folder must still get a high-water mark to avoid repeated full scans.
                    if initial_high_water is not None and len(batch_uids) == len(uids):
                        state.last_imap_uid = self._later_uid(state.last_imap_uid, initial_high_water)
                    self._record_folder_success(state=state)
                    self.db.commit()
        return HubMailboxImapSyncSummary(**summary)

    def _state(self, *, account: HubMailboxAccount, folder: str) -> HubMailboxImapSyncState:
        state = self.db.scalar(
            select(HubMailboxImapSyncState).where(
                HubMailboxImapSyncState.mailbox_account_id == account.id,
                HubMailboxImapSyncState.folder == folder,
            )
        )
        if state is not None:
            return state
        state = HubMailboxImapSyncState(mailbox_account_id=account.id, folder=folder)
        self.db.add(state)
        self.db.commit()
        return state

    def _new_uids(
        self,
        *,
        account: HubMailboxAccount,
        folder: str,
        state: HubMailboxImapSyncState,
    ) -> tuple[tuple[str, ...], str | None]:
        if state.last_imap_uid:
            return self._uids_after(account=account, folder=folder, last_uid=state.last_imap_uid), None
        prior_uids = self.db.scalars(
            select(HubMailboxImapImportItem.imap_uid).where(
                HubMailboxImapImportItem.mailbox_account_id == account.id,
                HubMailboxImapImportItem.folder == folder,
                HubMailboxImapImportItem.status.in_(("imported", "skipped")),
            )
        ).all()
        prior_high_water = self._highest_uid(prior_uids)
        if prior_high_water is not None:
            state.last_imap_uid = prior_high_water
            self.db.commit()
            return self._uids_after(account=account, folder=folder, last_uid=prior_high_water), None

        # A newly configured mailbox starts at the agreed independent-mail date, not its entire archive.
        recent_uids = self.importer._list_uids(account=account, folder=folder, since_date=_INITIAL_SYNC_DATE)
        return recent_uids, self._highest_current_uid(account=account, folder=folder)

    def _uids_after(self, *, account: HubMailboxAccount, folder: str, last_uid: str) -> tuple[str, ...]:
        first_uid = int(last_uid) + 1 if last_uid.isdigit() else 1
        with self.importer._connected_imap(account) as imap:
            status, _data = imap.select(folder, readonly=True)
            if status != "OK":
                raise HubMailboxImapImportError(f"Der IMAP-Ordner {folder} konnte nicht geöffnet werden.")
            status, data = imap.uid("SEARCH", None, "UID", f"{first_uid}:*")
            if status != "OK":
                raise HubMailboxImapImportError(f"Der IMAP-Ordner {folder} konnte nicht durchsucht werden.")
        return self._uids_from_response(data)

    def _highest_current_uid(self, *, account: HubMailboxAccount, folder: str) -> str | None:
        with self.importer._connected_imap(account) as imap:
            status, _data = imap.select(folder, readonly=True)
            if status != "OK":
                raise HubMailboxImapImportError(f"Der IMAP-Ordner {folder} konnte nicht geöffnet werden.")
            status, data = imap.uid("SEARCH", None, "ALL")
            if status != "OK":
                raise HubMailboxImapImportError(f"Der IMAP-Ordner {folder} konnte nicht durchsucht werden.")
        return self._highest_uid(self._uids_from_response(data))

    @staticmethod
    def _uids_from_response(data: list[object]) -> tuple[str, ...]:
        raw = data[0] if data else b""
        values = raw.decode("ascii", errors="ignore").split() if isinstance(raw, bytes) else str(raw or "").split()
        return tuple(uid for uid in values if uid.isdigit())

    @staticmethod
    def _highest_uid(values: list[str] | tuple[str, ...]) -> str | None:
        numeric = [int(value) for value in values if value.isdigit()]
        return str(max(numeric)) if numeric else None

    @staticmethod
    def _later_uid(current: str | None, candidate: str) -> str:
        if not current or not current.isdigit() or int(candidate) > int(current):
            return candidate
        return current

    def _record_folder_error(self, *, state: HubMailboxImapSyncState, error: str) -> None:
        refreshed = self.db.get(HubMailboxImapSyncState, state.id)
        if refreshed is None:
            return
        refreshed.last_synced_at = datetime.now(UTC)
        refreshed.last_error = error[:1_000]
        refreshed.consecutive_failures += 1
        self.db.commit()

    @staticmethod
    def _record_folder_success(*, state: HubMailboxImapSyncState) -> None:
        now = datetime.now(UTC)
        state.last_synced_at = now
        state.last_success_at = now
        state.last_error = None
        state.consecutive_failures = 0
        state.alerted_at = None
