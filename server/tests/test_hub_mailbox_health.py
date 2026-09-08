from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.models.hub_user import HubUser
from app.services.hub_mailbox import MAILBOX_HEALTH_ALERT_SOURCE
from app.services.hub_mailbox_health import HubMailboxHealthService


def _configured_account(cipher: SecretCipher) -> HubMailboxAccount:
    return HubMailboxAccount(
        email_address="info@kosmos-medien.de",
        display_name="Kosmos Medien",
        username="info@kosmos-medien.de",
        encrypted_password=cipher.encrypt("secret"),
        verified_at=datetime.now(UTC),
    )


def test_inbox_health_alert_is_sent_once_after_three_consecutive_failures(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        account = _configured_account(cipher)
        admin = HubUser(username="admin", password_hash="hash", reminder_email="admin@example.de")
        db.add_all([account, admin])
        db.flush()
        state = HubMailboxImapSyncState(
            mailbox_account_id=account.id,
            folder="INBOX",
            consecutive_failures=3,
            last_error="Die Mittwald-Verbindung wurde geschlossen.",
        )
        db.add(state)
        db.commit()

        sent: list[dict[str, object]] = []

        def fake_send(_self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(id=len(sent))

        monkeypatch.setattr("app.services.hub_mailbox_health.HubMailboxService.send_direct_email", fake_send)
        service = HubMailboxHealthService(db=db, cipher=cipher, public_base_url="https://hub.example.test")

        first = service.notify_unhealthy_inboxes()
        db.refresh(state)
        second = service.notify_unhealthy_inboxes()

        assert first.alerts_created == 1
        assert first.emails_sent == 1
        assert second.alerts_created == 0
        assert len(sent) == 1
        assert sent[0]["recipient_email"] == "admin@example.de"
        assert sent[0]["source"] == MAILBOX_HEALTH_ALERT_SOURCE
        assert state.alerted_at is not None


def test_inbox_health_alerts_after_five_minutes_without_a_success(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        account = _configured_account(cipher)
        admin = HubUser(username="admin", password_hash="hash", reminder_email="admin@example.de")
        db.add_all([account, admin])
        db.flush()
        state = HubMailboxImapSyncState(
            mailbox_account_id=account.id,
            folder="INBOX",
            last_success_at=datetime.now(UTC) - timedelta(minutes=6),
        )
        db.add(state)
        db.commit()

        monkeypatch.setattr(
            "app.services.hub_mailbox_health.HubMailboxService.send_direct_email",
            lambda _self, **_kwargs: SimpleNamespace(id=1),
        )
        summary = HubMailboxHealthService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
        ).notify_unhealthy_inboxes()

        assert summary.alerts_created == 1
        assert summary.emails_sent == 1
