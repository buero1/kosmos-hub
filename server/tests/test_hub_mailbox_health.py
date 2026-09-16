from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.models.hub_mailbox_imap_sync_failure import HubMailboxImapSyncFailure
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
        admin = HubUser(
            username="admin", password_hash="hash",
            reminder_email="info@kosmos-medien.de", mailbox_alert_email="admin@example.de",
        )
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
        admin = HubUser(username="admin", password_hash="hash", mailbox_alert_email="admin@example.de")
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


def test_pending_message_failure_is_alerted_once_even_when_sync_resumes(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        account = _configured_account(cipher)
        db.add_all([
            account,
            HubUser(username="admin", password_hash="hash", mailbox_alert_email="admin@example.de"),
        ])
        db.flush()
        db.add_all([
            HubMailboxImapSyncState(
                mailbox_account_id=account.id, folder="INBOX", last_success_at=datetime.now(UTC),
            ),
            HubMailboxImapSyncFailure(
                mailbox_account_id=account.id, folder="INBOX", imap_uid="46194",
                error="Nachricht überschreitet eine Datenbank-Feldgröße.", last_failed_at=datetime.now(UTC),
            ),
        ])
        db.commit()
        sent = []
        monkeypatch.setattr(
            "app.services.hub_mailbox_health.HubMailboxService.send_direct_email",
            lambda _self, **kwargs: sent.append(kwargs) or SimpleNamespace(id=1),
        )
        service = HubMailboxHealthService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        assert service.notify_unhealthy_inboxes().emails_sent == 1
        assert service.notify_unhealthy_inboxes().emails_sent == 0
        assert len(sent) == 1
        assert "46194" in sent[0]["content"]


def test_monitored_address_cannot_receive_its_own_alert():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add_all([
            _configured_account(cipher),
            HubUser(username="admin", password_hash="hash", mailbox_alert_email="info@kosmos-medien.de"),
        ])
        db.commit()
        service = HubMailboxHealthService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        assert service._recipient_emails() == ()
