from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerCallReminder, CustomerMeetingActivity, CustomerMeetingReminder
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.hub_user import HubUser
from app.services.customer_desktop_reminders import SNOOZE_MINUTES_OPTIONS, CustomerDesktopReminderService, DesktopReminderError


def test_due_popup_reminders_are_materialized_and_can_be_snoozed_or_completed(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 6, 10, 0)
    monkeypatch.setattr(CustomerDesktopReminderService, "_utc_now", staticmethod(lambda: now))

    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add_all([user, customer])
        db.flush()
        call = CustomerCallActivity(
            customer_id=customer.id,
            name="Rückruf",
            status="planned",
            direction="outbound",
            starts_at=now,
            ends_at=now + timedelta(minutes=30),
            duration_minutes=30,
            created_by_username=user.username,
            reminders=[CustomerCallReminder(channel="popup", minutes_before=0, sort_order=0)],
        )
        meeting = CustomerMeetingActivity(
            customer_id=customer.id,
            name="Projektstart",
            status="planned",
            starts_at=now + timedelta(minutes=5),
            ends_at=now + timedelta(minutes=65),
            created_by_username=user.username,
            reminders=[CustomerMeetingReminder(channel="popup", minutes_before=5, sort_order=0)],
        )
        db.add_all([call, meeting])
        db.flush()

        service = CustomerDesktopReminderService(db=db)
        due = service.list_due_reminders(user=user)

        assert [(item.activity_kind, item.activity_name) for item in due] == [
            ("call", "Rückruf"),
            ("meeting", "Projektstart"),
        ]
        assert due[0].due_at == now
        assert due[0].as_dict()["due_at"] == "2026-09-06T10:00:00+00:00"
        assert db.scalars(select(CustomerActivityReminderNotification)).all()

        assert service.snooze_reminders(user=user, notification_ids=[due[0].id], minutes=1) == 1
        assert [item.activity_name for item in service.list_due_reminders(user=user)] == ["Projektstart"]

        assert service.complete_reminders(user=user, notification_ids=[due[1].id]) == 1
        assert meeting.status == "completed"
        assert [item.activity_name for item in service.list_due_reminders(user=user)] == []


def test_desktop_reminders_reject_invalid_snooze_choices_and_foreign_ids():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        other_user = HubUser(username="other-admin", password_hash="hash", role="admin")
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add_all([user, other_user, customer])
        db.flush()
        notification = CustomerActivityReminderNotification(
            user_id=user.id,
            customer_id=customer.id,
            activity_kind="call",
            activity_id=7,
            reminder_key="0",
            remind_at=datetime(2026, 9, 6, 10, 0),
        )
        db.add(notification)
        db.flush()
        service = CustomerDesktopReminderService(db=db)

        with pytest.raises(DesktopReminderError, match="Dauer"):
            service.snooze_reminders(user=user, notification_ids=[notification.id], minutes=7)
        with pytest.raises(DesktopReminderError, match="nicht mehr verfügbar"):
            service.complete_reminders(user=other_user, notification_ids=[notification.id])


def test_desktop_reminder_snooze_options_include_long_intervals():
    assert SNOOZE_MINUTES_OPTIONS[-9:] == (240, 480, 720, 1440, 2880, 4320, 5760, 10080, 20160)


def test_desktop_reminders_can_be_snoozed_until_before_their_individual_starts(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 6, 10, 0)
    monkeypatch.setattr(CustomerDesktopReminderService, "_utc_now", staticmethod(lambda: now))

    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add_all([user, customer])
        db.flush()
        call = CustomerCallActivity(
            customer_id=customer.id,
            name="Rückruf",
            status="planned",
            direction="outbound",
            starts_at=now + timedelta(minutes=40),
            ends_at=now + timedelta(minutes=70),
            duration_minutes=30,
            created_by_username=user.username,
            reminders=[CustomerCallReminder(channel="popup", minutes_before=40, sort_order=0)],
        )
        db.add(call)
        db.flush()

        service = CustomerDesktopReminderService(db=db)
        due = service.list_due_reminders(user=user)

        assert service.snooze_reminders_before_start(
            user=user,
            notification_ids=[due[0].id],
            minutes_before=5,
        ) == 1
        notification = db.get(CustomerActivityReminderNotification, due[0].id)
        assert notification.snoozed_until == now + timedelta(minutes=35)

        assert service.snooze_reminders_before_start(
            user=user,
            notification_ids=[due[0].id],
            minutes_before=0,
        ) == 1
        assert notification.snoozed_until == now + timedelta(minutes=40)
