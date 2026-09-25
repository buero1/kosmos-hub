from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerCallReminder, CustomerMeetingActivity, CustomerMeetingReminder, CustomerTaskActivity
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.hub_user import HubUser
from app.services.customer_desktop_reminders import SNOOZE_MINUTES_OPTIONS, CustomerDesktopReminderService, DesktopReminderError
from app.services.hub_leads import HubLeadService


@pytest.mark.parametrize("kind", ["call", "meeting", "task"])
@pytest.mark.parametrize("related_kind", ["customer", "lead"])
def test_named_record_links_for_each_activity_kind(monkeypatch, kind, related_kind):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 25, 10, 0)
    monkeypatch.setattr(CustomerDesktopReminderService, "_utc_now", staticmethod(lambda: now))
    with Session(engine) as db:
        user = HubUser(username="admin", password_hash="hash", role="admin")
        db.add(user)
        customer = Customer(name="Example & Partner", is_visible=True)
        db.add(customer)
        lead = HubLeadService(db=db, cipher=get_secret_cipher()).create_lead(submitted_values={
            "lead_field__first_name": "Erika", "lead_field__last_name": "Example",
        })
        db.flush()
        link = {"customer_id": customer.id} if related_kind == "customer" else {"lead_id": lead.id}
        common = dict(**link, name="Test reminder", status="planned", assignee_user=user, created_by_username=user.username)
        if kind == "task":
            activity = CustomerTaskActivity(**common, due_at=now, reminder_channel="popup", reminder_minutes_before=0)
        elif kind == "call":
            activity = CustomerCallActivity(**common, starts_at=now, ends_at=now + timedelta(minutes=30), duration_minutes=30,
                                           direction="outbound", reminders=[CustomerCallReminder(channel="popup", minutes_before=0, sort_order=0)])
        else:
            activity = CustomerMeetingActivity(**common, starts_at=now, ends_at=now + timedelta(minutes=30),
                                              reminders=[CustomerMeetingReminder(channel="popup", minutes_before=0, sort_order=0)])
        db.add(activity)
        db.flush()
        service = CustomerDesktopReminderService(db=db)
        for materialize in (False, True):
            view, = service.list_due_reminders(user=user, materialize=materialize)
            assert view.as_dict()["related_name"] == (customer.name if related_kind == "customer" else "Erika Example")
            assert view.as_dict()["related_url"] == (f"/customers/{customer.id}" if related_kind == "customer" else f"/leads/{lead.id}")
            assert view.as_dict()["activity_url"] == f"/activities/{kind}/{activity.id}"


def test_lead_names_are_resolved_once_per_delivery(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 25, 10, 0)
    monkeypatch.setattr(CustomerDesktopReminderService, "_utc_now", staticmethod(lambda: now))
    with Session(engine) as db:
        user = HubUser(username="admin", password_hash="hash", role="admin")
        db.add(user)
        lead = HubLeadService(db=db, cipher=get_secret_cipher()).create_lead(submitted_values={"lead_field__last_name": "Example"})
        for name in ("First task", "Second task"):
            db.add(CustomerTaskActivity(lead_id=lead.id, name=name, status="planned", assignee_user=user,
                                       created_by_username=user.username, due_at=now, reminder_channel="popup", reminder_minutes_before=0))
        db.flush()
        original = HubLeadService.get_detail
        calls = []

        def detail(service, *, lead_id):
            calls.append(lead_id)
            return original(service, lead_id=lead_id)

        monkeypatch.setattr(HubLeadService, "get_detail", detail)
        assert len(CustomerDesktopReminderService(db=db).list_due_reminders(user=user)) == 2
        assert calls == [lead.id]


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
            assignee_user=user,
            reminders=[CustomerCallReminder(channel="popup", minutes_before=0, sort_order=0)],
        )
        meeting = CustomerMeetingActivity(
            customer_id=customer.id,
            name="Projektstart",
            status="planned",
            starts_at=now + timedelta(minutes=5),
            ends_at=now + timedelta(minutes=65),
            created_by_username=user.username,
            assignee_user=user,
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
        assert due[0].as_dict()["activity_url"] == f"/activities/call/{call.id}"
        assert due[0].as_dict()["related_url"] == f"/customers/{customer.id}"
        assert due[0].as_dict()["related_label"] == "Kunde"
        assert due[0].as_dict()["related_name"] == "Stop & Shop"
        assert due[0].as_dict()["activity_id"] == call.id
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


def test_unlinked_call_has_activity_link_but_no_related_link(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 6, 10, 0)
    monkeypatch.setattr(CustomerDesktopReminderService, "_utc_now", staticmethod(lambda: now))

    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        call = CustomerCallActivity(
            name="Rückruf ohne Kundenbezug",
            status="planned",
            direction="outbound",
            starts_at=now,
            ends_at=now + timedelta(minutes=30),
            duration_minutes=30,
            created_by_username=user.username,
            assignee_user=user,
            reminders=[CustomerCallReminder(channel="popup", minutes_before=0, sort_order=0)],
        )
        db.add_all([user, call])
        db.flush()

        reminder = CustomerDesktopReminderService(db=db).list_due_reminders(user=user)[0].as_dict()

        assert reminder["activity_url"] == f"/activities/call/{call.id}"
        assert reminder["related_url"] is None
        assert reminder["related_label"] is None
        assert reminder["related_name"] is None


def test_desktop_reminder_uses_current_task_customer_after_relink(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 6, 10, 0)
    monkeypatch.setattr(CustomerDesktopReminderService, "_utc_now", staticmethod(lambda: now))

    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        first_customer = Customer(name="Alt", is_visible=True)
        current_customer = Customer(name="Neu", is_visible=True)
        db.add_all([user, first_customer, current_customer])
        db.flush()
        task = CustomerTaskActivity(
            customer_id=current_customer.id,
            name="Aufgabe",
            status="planned",
            due_at=now,
            reminder_channel="popup",
            reminder_minutes_before=0,
            created_by_username=user.username,
            assignee_user=user,
        )
        db.add(task)
        db.flush()
        db.add(CustomerActivityReminderNotification(
            user_id=user.id,
            customer_id=first_customer.id,
            activity_kind="task",
            activity_id=task.id,
            reminder_key="primary",
            remind_at=now,
        ))
        db.flush()

        reminder = CustomerDesktopReminderService(db=db).list_due_reminders(user=user)[0].as_dict()

        assert reminder["activity_url"] == f"/activities/task/{task.id}"
        assert reminder["related_url"] == f"/customers/{current_customer.id}"


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
            assignee_user=user,
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
