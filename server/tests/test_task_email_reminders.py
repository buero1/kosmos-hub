import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity import CustomerTaskActivity
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.hub_mailbox import TASK_EMAIL_REMINDER_SOURCE
from app.services.hub_mailbox_transport import HubMailboxTransportDelivery, HubMailboxTransportError, HubMailboxTransportService
from app.services.task_email_reminders import TaskEmailReminderError, TaskEmailReminderService


def _prepared_reminder(db: Session, *, now: datetime) -> tuple[CustomerTaskActivity, CustomerTaskEmailReminder, SecretCipher]:
    cipher = SecretCipher("a" * 32)
    user = HubUser(username="hub-admin", password_hash="hash", reminder_email="team@example.de")
    customer = Customer(name="Stop & Shop", is_visible=True)
    sender = HubMailboxAccount(
        email_address="info@kosmos-medien.de",
        display_name="Kosmos Medien",
        username="info@kosmos-medien.de",
        encrypted_password=cipher.encrypt("secret"),
        verified_at=now.replace(tzinfo=UTC),
        enabled=True,
    )
    db.add_all([user, customer, sender])
    db.flush()
    task = CustomerTaskActivity(
        customer_id=customer.id,
        name="Angebot nachfassen",
        status="planned",
        due_at=now + timedelta(minutes=15),
        reminder_channel="email",
        reminder_minutes_before=15,
        description="Kundin anrufen.",
        created_by_username=user.username,
    )
    db.add(task)
    db.flush()
    reminder = TaskEmailReminderService(db=db).sync_task(task=task)
    assert reminder is not None
    db.commit()
    return task, reminder, cipher


def test_due_task_email_reminder_is_sent_once_and_recorded(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 8, 8, 0)
    monkeypatch.setattr(TaskEmailReminderService, "_utc_now", staticmethod(lambda: now))
    deliveries: list[dict[str, object]] = []

    def fake_send(self, **kwargs):
        deliveries.append(kwargs)
        return HubMailboxTransportDelivery(message_id=kwargs["message_id"], sent_at=now.replace(tzinfo=UTC))

    monkeypatch.setattr(HubMailboxTransportService, "send", fake_send)
    with Session(engine) as db:
        _task, reminder, cipher = _prepared_reminder(db, now=now)
        result = TaskEmailReminderService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
        ).process_due_reminders()

        db.refresh(reminder)
        assert result.sent == 1
        assert reminder.status == "sent"
        assert reminder.sent_at == now
        assert reminder.mailbox_email_id is not None
        assert deliveries[0]["sender_email"] == "info@kosmos-medien.de"
        assert deliveries[0]["recipient_email"] == "team@example.de"
        assert deliveries[0]["message_id"] == reminder.message_id
        stored_email = db.get(HubMailboxEmail, reminder.mailbox_email_id)
        assert stored_email is not None
        assert stored_email.source == TASK_EMAIL_REMINDER_SOURCE
        sent_html = json.loads(cipher.decrypt(stored_email.encrypted_payload_json))["content"]
        assert f'href="https://hub.example.test/customers/{_task.customer_id}#task-{_task.id}"' in sent_html
        assert f'href="https://hub.example.test/customers/{_task.customer_id}"' in sent_html
        assert 'target="_blank"' in sent_html
        assert "Aufgabe im Hub öffnen" not in sent_html

        assert TaskEmailReminderService(db=db, cipher=cipher).process_due_reminders().sent == 0


def test_failed_delivery_is_retried_with_the_same_message_id(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 8, 8, 0)
    monkeypatch.setattr(TaskEmailReminderService, "_utc_now", staticmethod(lambda: now))
    message_ids: list[str] = []

    def failing_send(self, **kwargs):
        message_ids.append(kwargs["message_id"])
        raise HubMailboxTransportError("SMTP nicht erreichbar")

    monkeypatch.setattr(HubMailboxTransportService, "send", failing_send)
    with Session(engine) as db:
        _task, reminder, cipher = _prepared_reminder(db, now=now)
        service = TaskEmailReminderService(db=db, cipher=cipher)

        assert service.process_due_reminders().retried == 1
        db.refresh(reminder)
        assert reminder.status == "retrying"
        assert reminder.attempt_count == 1
        assert reminder.next_attempt_at == now + timedelta(minutes=5)
        assert message_ids == [reminder.message_id]


def test_missing_personal_address_blocks_email_reminder_creation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash")
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add_all([user, customer])
        db.flush()
        task = CustomerTaskActivity(
            customer_id=customer.id,
            name="Nachfassen",
            status="planned",
            due_at=datetime(2026, 9, 8, 8, 0),
            reminder_channel="email",
            reminder_minutes_before=0,
            created_by_username=user.username,
        )
        db.add(task)
        db.flush()

        with pytest.raises(TaskEmailReminderError, match="Erinnerungsadresse"):
            TaskEmailReminderService(db=db).sync_task(task=task)


def test_completed_or_deleted_task_cancels_the_open_delivery():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 8, 8, 0)
    with Session(engine) as db:
        task, reminder, _cipher = _prepared_reminder(db, now=now)
        task.status = "completed"
        TaskEmailReminderService(db=db).sync_task(task=task)
        assert reminder.status == "cancelled"

        task.status = "planned"
        TaskEmailReminderService(db=db).sync_task(task=task)
        assert reminder.status == "scheduled"
        TaskEmailReminderService(db=db).cancel_for_deleted_task(task=task)
        assert reminder.status == "cancelled"
        assert reminder.task_id is None
        assert db.scalar(select(CustomerTaskEmailReminder.id)) == reminder.id


def test_moving_a_task_reopens_an_already_sent_email_reminder():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 8, 8, 0)
    with Session(engine) as db:
        task, reminder, _cipher = _prepared_reminder(db, now=now)
        reminder.status = "sent"
        reminder.sent_at = now
        reminder.mailbox_email_id = 123
        original_message_id = reminder.message_id
        task.due_at += timedelta(hours=1)

        reopened = TaskEmailReminderService(db=db).sync_task(task=task)

        assert reopened is reminder
        assert reminder.status == "scheduled"
        assert reminder.scheduled_at == now + timedelta(hours=1)
        assert reminder.next_attempt_at == now + timedelta(hours=1)
        assert reminder.sent_at is None
        assert reminder.mailbox_email_id is None
        assert reminder.message_id != original_message_id


def test_editing_a_sent_task_without_moving_it_does_not_create_a_duplicate_reminder():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 8, 8, 0)
    with Session(engine) as db:
        task, reminder, _cipher = _prepared_reminder(db, now=now)
        reminder.status = "sent"
        reminder.sent_at = now
        original_message_id = reminder.message_id
        task.description = "Nur die Beschreibung wurde ergänzt."

        TaskEmailReminderService(db=db).sync_task(task=task)

        assert reminder.status == "sent"
        assert reminder.message_id == original_message_id
