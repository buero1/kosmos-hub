import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_finance_documents import HubFinanceDunning
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_user import HubUser
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_scheduled_email import HubScheduledEmail
from app.services.customer_communications import (
    CustomerCommunicationAttachmentUpload,
    CustomerCommunicationService,
)
from app.services.email_attachment_storage import EmailAttachmentStorage, EmailAttachmentStorageError
from app.services.hub_mailbox_transport import HubMailboxTransportDelivery, HubMailboxTransportService
from app.services.hub_mailbox import HubMailboxService
from app.services.scheduled_emails import ScheduledEmailService


def _cipher() -> SecretCipher:
    return SecretCipher("s" * 32)


def _storage(tmp_path, cipher: SecretCipher) -> EmailAttachmentStorage:
    return EmailAttachmentStorage(root=tmp_path / "attachments", cipher=cipher, min_free_bytes=0)


def _sender(db: Session, cipher: SecretCipher, now: datetime) -> None:
    from sqlalchemy import select
    from mailbox_fixture_helpers import mailbox_account
    if db.scalar(select(HubUser).where(HubUser.username == "hub-admin")) is None:
        db.add(HubUser(username="hub-admin", password_hash="x", role="admin"))
    mailbox_account(db, cipher)


def test_browser_schedule_is_interpreted_as_berlin_time():
    assert ScheduledEmailService.parse_berlin_datetime("2026-09-18T12:30") == datetime(2026, 9, 18, 10, 30)


def test_direct_scheduled_email_is_encrypted_and_delivered_once(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    storage = _storage(tmp_path, cipher)
    now = datetime(2026, 9, 18, 8, 0)
    clock = {"now": now}
    monkeypatch.setattr(ScheduledEmailService, "_utc_now", staticmethod(lambda: clock["now"]))
    deliveries: list[dict[str, object]] = []

    def fake_send(self, **kwargs):
        deliveries.append(kwargs)
        return HubMailboxTransportDelivery(message_id=kwargs["message_id"], sent_at=clock["now"].replace(tzinfo=UTC))

    monkeypatch.setattr(HubMailboxTransportService, "send", fake_send)
    with Session(engine) as db:
        _sender(db, cipher, now)
        service = ScheduledEmailService(db=db, cipher=cipher, attachment_storage=storage)
        scheduled = service.schedule(
            actor="hub-admin",
            scheduled_at=now + timedelta(hours=1),
            sender_email="info@kosmos-medien.de",
            recipient_email="kunde@example.de",
            recipient_name="Kunde",
            subject="Geplanter Versand",
            content="<p>Vertraulicher Inhalt</p>",
            cc_emails="",
            attachments=(
                CustomerCommunicationAttachmentUpload(
                    filename="info.txt",
                    content=b"secret attachment",
                    content_type="text/plain",
                ),
            ),
        )
        db.commit()

        assert scheduled.status == "scheduled"
        assert "Vertraulicher Inhalt" not in scheduled.encrypted_payload_json
        storage_key = scheduled.attachments[0].storage_key
        assert storage.load(storage_key) == b"secret attachment"
        assert service.process_due().sent == 0

        clock["now"] = now + timedelta(hours=1)
        assert service.process_due().sent == 1
        db.refresh(scheduled)
        assert scheduled.status == "sent"
        assert scheduled.mailbox_email_id is not None
        assert deliveries[0]["message_id"] == scheduled.message_id
        sent = db.get(HubMailboxEmail, scheduled.mailbox_email_id)
        assert sent is not None
        assert json.loads(cipher.decrypt(sent.encrypted_payload_json))["subject"] == "Geplanter Versand"
        with pytest.raises(EmailAttachmentStorageError):
            storage.load(storage_key)

        assert service.process_due().sent == 0
        assert len(deliveries) == 1


def test_customer_email_is_visible_as_planned_until_delivery(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    storage = _storage(tmp_path, cipher)
    now = datetime(2026, 9, 18, 8, 0)
    monkeypatch.setattr(ScheduledEmailService, "_utc_now", staticmethod(lambda: now))
    with Session(engine) as db:
        _sender(db, cipher, now)
        customer = Customer(
            name="Beispielkunde",
            is_visible=True,
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Kontakt-E-Mail": "kunde@example.de"}})),
        )
        db.add(customer)
        db.flush()
        scheduled = ScheduledEmailService(
            db=db,
            cipher=cipher,
            attachment_storage=storage,
        ).schedule(
            actor="hub-admin",
            scheduled_at=now + timedelta(hours=2),
            sender_email="info@kosmos-medien.de",
            recipient_email="kunde@example.de",
            recipient_name="Beispielkunde",
            recipient_key="account:kunde@example.de",
            subject="Terminbestätigung",
            content="<p>Bis bald</p>",
            cc_emails="",
            customer_id=customer.id,
        )
        db.commit()

        view = CustomerCommunicationService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            attachment_storage=storage,
        ).get_view(customer_id=customer.id)

        assert len(view.emails) == 1
        assert view.emails[0].id == -scheduled.id
        assert view.emails[0].source == "scheduled"
        assert view.emails[0].sync_status == "scheduled"
        assert view.emails[0].subject == "Terminbestätigung"
        assert view.emails[0].occurred_at == now + timedelta(hours=2)


def test_dunning_email_link_moves_from_planned_to_sent_message(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    storage = _storage(tmp_path, cipher)
    now = datetime(2026, 9, 18, 8, 0)
    clock = {"now": now}
    monkeypatch.setattr(ScheduledEmailService, "_utc_now", staticmethod(lambda: clock["now"]))
    monkeypatch.setattr(
        HubMailboxTransportService,
        "send",
        lambda self, **kwargs: HubMailboxTransportDelivery(
            message_id=kwargs["message_id"],
            sent_at=clock["now"].replace(tzinfo=UTC),
        ),
    )

    with Session(engine) as db:
        _sender(db, cipher, now)
        customer = Customer(
            name="Beispielkunde",
            is_visible=True,
            encrypted_profile_json=cipher.encrypt(
                json.dumps({"fields": {"Kontakt-E-Mail": "kunde@example.de"}})
            ),
        )
        db.add(customer)
        db.flush()
        dunning = HubFinanceDunning(
            customer_id=customer.id,
            encrypted_fields_json=cipher.encrypt(json.dumps({"status": "open"})),
        )
        db.add(dunning)
        db.flush()

        service = ScheduledEmailService(
            db=db,
            cipher=cipher,
            attachment_storage=storage,
        )
        scheduled = service.schedule(
            actor="hub-admin",
            scheduled_at=now + timedelta(hours=1),
            sender_email="info@kosmos-medien.de",
            recipient_email="kunde@example.de",
            recipient_name="Beispielkunde",
            recipient_key="account:kunde@example.de",
            subject="Zahlungserinnerung",
            content="<p>Bitte prüfen Sie die Mahnung.</p>",
            cc_emails="",
            customer_id=customer.id,
            dunning_id=dunning.id,
        )
        db.commit()

        communications = CustomerCommunicationService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            attachment_storage=storage,
        )
        planned = communications.get_dunning_email_views(dunning_id=dunning.id)
        assert len(planned) == 1
        assert planned[0].source == "scheduled"
        assert service.get_compose_context(scheduled_email_id=scheduled.id)["dunning_id"] == dunning.id

        clock["now"] = now + timedelta(hours=1)
        assert service.process_due().sent == 1
        db.refresh(scheduled)

        sent_email = db.get(CustomerZohoEmail, scheduled.customer_email_id)
        assert sent_email is not None
        assert sent_email.dunning_id == dunning.id
        linked = communications.get_dunning_email_views(dunning_id=dunning.id)
        assert len(linked) == 1
        assert linked[0].source == "hub"
        assert linked[0].subject == "Zahlungserinnerung"


def test_past_schedule_is_rejected(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    now = datetime(2026, 9, 18, 8, 0)
    with Session(engine) as db:
        _sender(db, cipher, now)
        service = ScheduledEmailService(db=db, cipher=cipher, attachment_storage=_storage(tmp_path, cipher))
        service._utc_now = lambda: now  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="Zukunft"):
            service.schedule(
                actor="hub-admin",
                scheduled_at=now,
                sender_email="info@kosmos-medien.de",
                recipient_email="kunde@example.de",
                recipient_name="",
                subject="Zu spät",
                content="Nachricht",
                cc_emails="",
            )


def test_planned_mailbox_supports_edit_attachment_download_and_cancel(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    storage = _storage(tmp_path, cipher)
    now = datetime(2026, 9, 18, 8, 0)
    monkeypatch.setattr(ScheduledEmailService, "_utc_now", staticmethod(lambda: now))

    with Session(engine) as db:
        _sender(db, cipher, now)
        service = ScheduledEmailService(db=db, cipher=cipher, attachment_storage=storage)
        scheduled = service.schedule(
            actor="hub-admin",
            scheduled_at=now + timedelta(hours=3),
            sender_email="info@kosmos-medien.de",
            recipient_email="kunde@example.de",
            recipient_name="Kunde",
            subject="Erste Fassung",
            content="<p>Geplanter Inhalt</p>",
            cc_emails="",
            attachments=(
                CustomerCommunicationAttachmentUpload(
                    filename="planung.txt",
                    content=b"planung",
                    content_type="text/plain",
                ),
            ),
        )
        db.commit()
        attachment_id = scheduled.attachments[0].id
        storage_key = scheduled.attachments[0].storage_key

        mailbox = HubMailboxService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            attachment_storage=storage,
        ).get_view(folder="planned", unread_only=False, selected_key=f"scheduled-{scheduled.id}")
        assert mailbox.folder_counts["planned"] == 1
        assert mailbox.selected is not None
        assert mailbox.selected.kind == "scheduled"
        assert mailbox.selected.mailbox_state == "scheduled"
        assert mailbox.selected.attachments[0].filename == "planung.txt"

        context = service.get_compose_context(scheduled_email_id=scheduled.id)
        assert context["subject"] == "Erste Fassung"
        assert context["scheduled_at"] == "2026-09-18T13:00"
        assert service.download_attachment(
            scheduled_email_id=scheduled.id,
            attachment_id=attachment_id,
        ).content == b"planung"

        updated = service.schedule(
            actor="hub-admin",
            scheduled_email_id=scheduled.id,
            scheduled_at=now + timedelta(hours=4),
            sender_email="info@kosmos-medien.de",
            recipient_email="kunde@example.de",
            recipient_name="Kunde",
            subject="Aktualisierte Fassung",
            content="<p>Aktualisiert</p>",
            cc_emails="",
        )
        db.commit()
        assert updated.id == scheduled.id
        assert updated.attachments[0].id == attachment_id
        assert service.get_compose_context(scheduled_email_id=scheduled.id)["subject"] == "Aktualisierte Fassung"

        service.cancel(scheduled_email_id=scheduled.id)
        db.commit()
        assert scheduled.status == "cancelled"
        assert HubMailboxService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            attachment_storage=storage,
        ).get_folder_counts()["planned"] == 0
        with pytest.raises(EmailAttachmentStorageError):
            storage.load(storage_key)
