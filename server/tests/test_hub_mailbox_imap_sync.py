from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.services import maintenance_worker
from app.services.hub_mailbox_imap_import import HubMailboxImapImportError
from app.services.hub_mailbox_imap_sync import HubMailboxImapSyncService


def test_incremental_sync_only_processes_uids_after_the_saved_high_water_mark():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        account = HubMailboxAccount(
            email_address="info@kosmos.example",
            display_name="Kosmos Hub",
            username="info@kosmos.example",
            encrypted_password=cipher.encrypt("secret"),
            verified_at=datetime.now(UTC),
        )
        db.add(account)
        db.flush()
        state = HubMailboxImapSyncState(mailbox_account_id=account.id, folder="INBOX", last_imap_uid="100")
        db.add(state)
        db.commit()

        service = HubMailboxImapSyncService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        service._new_uids = lambda **kwargs: (("101", "102"), None) if kwargs["folder"] == "INBOX" else ((), None)
        service.importer._fetch_message = lambda **_kwargs: (b"raw", "")
        service.importer._parse_message = lambda **_kwargs: object()
        service.importer._store_message = lambda **_kwargs: ("imported", 0, 0)

        summary = service.sync_once()
        db.refresh(state)

        assert summary.checked == 2
        assert summary.imported == 2
        assert summary.failed == 0
        assert state.last_imap_uid == "102"
        assert state.last_success_at is not None
        assert state.consecutive_failures == 0
        assert state.alerted_at is None


def test_incremental_sync_can_limit_a_pass_to_the_sent_folder():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        account = HubMailboxAccount(
            email_address="info@kosmos.example",
            display_name="Kosmos Hub",
            username="info@kosmos.example",
            encrypted_password=cipher.encrypt("secret"),
            verified_at=datetime.now(UTC),
        )
        db.add(account)
        db.commit()

        checked_folders: list[str] = []
        service = HubMailboxImapSyncService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        service._new_uids = lambda **kwargs: (checked_folders.append(kwargs["folder"]) or (), None)

        summary = service.sync_once(folders=("INBOX.Sent",))

        assert summary.checked == 0
        assert checked_folders == ["INBOX.Sent"]


def test_incremental_sync_records_a_failure_streak_and_resets_it_after_a_success():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        account = HubMailboxAccount(
            email_address="info@kosmos.example",
            display_name="Kosmos Hub",
            username="info@kosmos.example",
            encrypted_password=cipher.encrypt("secret"),
            verified_at=datetime.now(UTC),
        )
        db.add(account)
        db.commit()

        service = HubMailboxImapSyncService(db=db, cipher=cipher, public_base_url="https://hub.example.test")

        def fail_uids(**_kwargs):
            raise HubMailboxImapImportError("Mittwald ist nicht erreichbar.")

        service._new_uids = fail_uids
        summary = service.sync_once(folders=("INBOX",))
        state = db.scalar(
            select(HubMailboxImapSyncState).where(
                HubMailboxImapSyncState.mailbox_account_id == account.id,
                HubMailboxImapSyncState.folder == "INBOX",
            )
        )

        assert summary.failed == 1
        assert state is not None
        assert state.consecutive_failures == 1
        assert state.last_success_at is None
        assert state.last_error == "Mittwald ist nicht erreichbar."

        state.alerted_at = datetime.now(UTC)
        db.commit()
        service._new_uids = lambda **_kwargs: ((), None)
        summary = service.sync_once(folders=("INBOX",))
        db.refresh(state)

        assert summary.failed == 0
        assert state.last_success_at is not None
        assert state.consecutive_failures == 0
        assert state.alerted_at is None


def test_imap_idle_waits_for_an_inbox_change_then_ends_the_session(monkeypatch):
    class FakeImap:
        capabilities = ("IMAP4rev1", "IDLE")

        def __init__(self) -> None:
            self.sock = object()
            self.sent: list[bytes] = []
            self.lines = [b"+ idling\r\n", b"* 4 EXISTS\r\n", b"A001 OK IDLE terminated\r\n"]

        @staticmethod
        def _new_tag() -> bytes:
            return b"A001"

        def send(self, payload: bytes) -> None:
            self.sent.append(payload)

        def readline(self) -> bytes:
            return self.lines.pop(0)

    imap = FakeImap()
    monkeypatch.setattr(maintenance_worker, "select_socket", lambda *_args: ([imap.sock], [], []))

    changed = maintenance_worker._wait_for_hub_mailbox_idle_change(imap, duration_seconds=30)

    assert changed is True
    assert imap.sent == [b"A001 IDLE\r\n", b"DONE\r\n"]


def test_imap_idle_requires_server_support():
    class FakeImap:
        capabilities = ("IMAP4rev1",)

    with pytest.raises(HubMailboxImapImportError, match="IDLE"):
        maintenance_worker._wait_for_hub_mailbox_idle_change(FakeImap(), duration_seconds=30)
