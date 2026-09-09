from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.routes.web import _next_task_due_date
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_user import HubUser
from app.services.customer_activities import CALL_REMINDER_OPTIONS, CALL_TIME_OPTIONS, CustomerActivityError, CustomerActivityService, suggested_call_start


def test_completing_calls_and_tasks_requires_confirmation_in_customer_activities():
    template = Path("app/templates/customer_detail.html").read_text(encoding="utf-8")
    script = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert "data-customer-call-complete-open" in template
    assert "data-customer-task-complete-open" in template
    assert "data-customer-call-complete-dialog" in template
    assert "data-customer-task-complete-dialog" in template
    assert "data-customer-call-complete-open" in script
    assert "data-customer-task-complete-open" in script


def test_customer_activity_modules_have_separate_tables():
    assert {
        "customer_call_activities",
        "customer_task_activities",
        "customer_task_email_reminders",
        "customer_meeting_activities",
        "customer_meeting_reminders",
    }.issubset(Base.metadata.tables)
    assert CALL_TIME_OPTIONS[0] == "00:00"
    assert CALL_TIME_OPTIONS[-1] == "23:30"
    assert len(CALL_TIME_OPTIONS) == 48
    assert CALL_REMINDER_OPTIONS[0] == 0


def test_new_task_default_is_the_next_calendar_day():
    berlin = ZoneInfo("Europe/Berlin")

    assert _next_task_due_date(datetime(2026, 9, 8, 0, 1, tzinfo=berlin)) == date(2026, 9, 9)
    assert _next_task_due_date(datetime(2026, 12, 31, 23, 59, tzinfo=berlin)) == date(2027, 1, 1)


def test_suggested_call_start_skips_a_slot_with_less_than_ten_minutes_remaining_at_any_time():
    berlin = ZoneInfo("Europe/Berlin")

    assert suggested_call_start(datetime(2026, 9, 6, 6, 20, tzinfo=berlin)).strftime("%H:%M") == "06:30"
    assert suggested_call_start(datetime(2026, 9, 6, 10, 20, tzinfo=berlin)).strftime("%H:%M") == "10:30"
    assert suggested_call_start(datetime(2026, 9, 6, 10, 20, 1, tzinfo=berlin)).strftime("%H:%M") == "11:00"
    assert suggested_call_start(datetime(2026, 9, 6, 18, 1, tzinfo=berlin)).strftime("%H:%M") == "18:30"
    assert suggested_call_start(datetime(2026, 9, 6, 18, 50, tzinfo=berlin)).strftime("%Y-%m-%d %H:%M") == "2026-09-06 19:00"
    assert suggested_call_start(datetime(2026, 9, 6, 18, 50, 1, tzinfo=berlin)).strftime("%Y-%m-%d %H:%M") == "2026-09-06 19:30"


def test_schedule_call_stores_berlin_time_as_utc_and_lists_it():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        user = HubUser(username="hub-admin", password_hash="hash", reminder_email="team@example.de")
        db.add_all([customer, user])
        db.flush()

        scheduled = CustomerActivityService(db=db).schedule_call(
            customer_id=customer.id,
            actor="hub-admin",
            name="Rückruf zur Bestellung",
            status="planned",
            direction="outbound",
            start_date="2026-09-06",
            start_time="12:00",
            duration_minutes="30",
            reminder_channels=["popup", "email", "none"],
            reminder_minutes_before=["5", "15", "5"],
            description="Termin abstimmen",
        )

        assert scheduled.starts_at == datetime(2026, 9, 6, 10, 0)
        assert scheduled.ends_at == datetime(2026, 9, 6, 10, 30)
        assert scheduled.reminder_channel == "popup"
        call = CustomerActivityService(db=db).list_calls(customer_id=customer.id)[0]
        assert call.name == "Rückruf zur Bestellung"
        assert [(reminder.channel, reminder.minutes_before) for reminder in call.reminders] == [("popup", 5), ("email", 15)]


def test_test_customer_can_receive_customer_activities_when_hidden_from_standard_lists():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        test_customer = Customer(name="Test-Kunde", is_visible=False)
        db.add(test_customer)
        db.flush()

        task = CustomerActivityService(db=db).schedule_task(
            customer_id=test_customer.id,
            actor="hub-admin",
            name="Test-Aufgabe",
            status="planned",
            due_date="2026-09-10",
            due_time="09:00",
            reminder_channel="popup",
            reminder_minutes_before="0",
            description="",
        )

        assert task.customer_id == test_customer.id


def test_calendar_calls_and_meetings_can_be_saved_without_a_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = CustomerActivityService(db=db)
        call = service.schedule_call(
            customer_id=None,
            actor="hub-admin",
            name="Freier Rückruf",
            status="planned",
            direction="outbound",
            start_date="2026-09-07",
            start_time="09:00",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["5"],
            description="",
        )
        meeting = service.schedule_meeting(
            customer_id=None,
            actor="hub-admin",
            name="Internes Meeting",
            status="planned",
            start_date="2026-09-07",
            start_time="10:00",
            duration_minutes="60",
            reminder_channels=["popup"],
            reminder_minutes_before=["15"],
            description="",
        )

        assert call.customer_id is None
        assert meeting.customer_id is None
        assert [(activity.name, activity.customer_id, activity.customer_name) for activity in service.list_calendar_activities(week_start=date(2026, 9, 7))] == [
            ("Freier Rückruf", None, ""),
            ("Internes Meeting", None, ""),
        ]


def test_calendar_activities_can_be_edited_and_linked_to_a_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add(customer)
        db.flush()
        service = CustomerActivityService(db=db)
        call = service.schedule_call(
            customer_id=None,
            actor="hub-admin",
            name="Freier Rückruf",
            status="planned",
            direction="outbound",
            start_date="2026-09-07",
            start_time="09:00",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["5"],
            description="Vorbereitung",
        )
        meeting = service.schedule_meeting(
            customer_id=None,
            actor="hub-admin",
            name="Internes Meeting",
            status="planned",
            start_date="2026-09-07",
            start_time="10:00",
            duration_minutes="60",
            reminder_channels=["popup"],
            reminder_minutes_before=["15"],
            description="Abstimmung",
        )

        updated_call = service.update_calendar_call(
            call_id=call.id,
            customer_id=customer.id,
            name="Rückruf zum Auftrag",
            status="completed",
            direction="inbound",
            start_date="2026-09-07",
            start_time="11:00",
            duration_minutes="45",
            reminder_channels=["email"],
            reminder_minutes_before=["10"],
            description="Besprochen",
        )
        updated_meeting = service.update_calendar_meeting(
            meeting_id=meeting.id,
            customer_id=None,
            name="Internes Meeting verschoben",
            status="planned",
            start_date="2026-09-07",
            start_time="13:00",
            duration_minutes="90",
            reminder_channels=["popup", "email"],
            reminder_minutes_before=["5", "15"],
            description="Neue Agenda",
        )

        assert updated_call.customer_id == customer.id
        assert updated_call.direction == "inbound"
        assert updated_meeting.customer_id is None
        activities = service.list_calendar_activities(week_start=date(2026, 9, 7))
        assert [(activity.name, activity.duration_minutes, activity.customer_name) for activity in activities] == [
            ("Rückruf zum Auftrag", 45, "Stop & Shop"),
            ("Internes Meeting verschoben", 90, ""),
        ]
        assert activities[0].reminder_channels == ("email",)
        assert activities[1].reminder_minutes_before == (5, 15)


def test_calendar_places_overlapping_activities_in_separate_columns():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = CustomerActivityService(db=db)
        for kind, name, start_time, duration in (
            ("call", "Erster Termin", "09:00", "60"),
            ("meeting", "Zweiter Termin", "09:30", "60"),
            ("call", "Dritter Termin", "09:30", "30"),
            ("meeting", "Folgetermin", "10:30", "30"),
        ):
            if kind == "call":
                service.schedule_call(
                    customer_id=None,
                    actor="hub-admin",
                    name=name,
                    status="planned",
                    direction="outbound",
                    start_date="2026-09-07",
                    start_time=start_time,
                    duration_minutes=duration,
                    reminder_channels=[],
                    reminder_minutes_before=[],
                    description="",
                )
            else:
                service.schedule_meeting(
                    customer_id=None,
                    actor="hub-admin",
                    name=name,
                    status="planned",
                    start_date="2026-09-07",
                    start_time=start_time,
                    duration_minutes=duration,
                    reminder_channels=[],
                    reminder_minutes_before=[],
                    description="",
                )

        activities = service.list_calendar_activities(week_start=date(2026, 9, 7))

        assert [(activity.name, activity.column_index, activity.column_count) for activity in activities] == [
            ("Erster Termin", 0, 3),
            ("Dritter Termin", 1, 3),
            ("Zweiter Termin", 2, 3),
            ("Folgetermin", 0, 1),
        ]


def test_schedule_call_rejects_an_invalid_duration():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add(customer)
        db.flush()

        with pytest.raises(CustomerActivityError, match="Dauer"):
            CustomerActivityService(db=db).schedule_call(
                customer_id=customer.id,
                actor="hub-admin",
                name="Rückruf",
                status="planned",
                direction="outbound",
                start_date="2026-09-06",
                start_time="12:00",
                duration_minutes="7",
                reminder_channels=["popup"],
                reminder_minutes_before=["5"],
                description="",
            )


def test_update_and_delete_call_keep_changes_scoped_to_its_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        other_customer = Customer(name="Other", is_visible=True)
        db.add_all([customer, other_customer])
        db.flush()
        service = CustomerActivityService(db=db)
        call = service.schedule_call(
            customer_id=customer.id,
            actor="hub-admin",
            name="Rückruf",
            status="planned",
            direction="outbound",
            start_date="2026-09-06",
            start_time="12:00",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["5"],
            description="Alt",
        )

        changed = service.update_call(
            customer_id=customer.id,
            call_id=call.id,
            name="Rückruf verschoben",
            status="completed",
            direction="inbound",
            start_date="2026-09-07",
            start_time="14:30",
            duration_minutes="60",
            reminder_channels=["email", "popup"],
            reminder_minutes_before=["0", "5"],
            description="Neu",
        )

        assert changed.name == "Rückruf verschoben"
        assert changed.starts_at == datetime(2026, 9, 7, 12, 30)
        assert [(reminder.channel, reminder.minutes_before) for reminder in changed.reminders] == [("email", 0), ("popup", 5)]
        with pytest.raises(CustomerActivityError, match="nicht gefunden"):
            service.delete_call(customer_id=other_customer.id, call_id=call.id)

        service.delete_call(customer_id=customer.id, call_id=call.id)
        assert service.list_calls(customer_id=customer.id) == ()


def test_schedule_task_uses_the_task_module_with_an_immediate_email_reminder():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        user = HubUser(username="hub-admin", password_hash="hash", reminder_email="team@example.de")
        db.add_all([customer, user])
        db.flush()

        task = CustomerActivityService(db=db).schedule_task(
            customer_id=customer.id,
            actor="hub-admin",
            name="Angebot nachfassen",
            status="planned",
            due_date="2026-09-06",
            due_time="09:00",
            reminder_channel="email",
            reminder_minutes_before="0",
            description="Kundin anrufen.",
        )

        assert task.due_at == datetime(2026, 9, 6, 7, 0)
        assert task.reminder_channel == "email"
        assert task.reminder_minutes_before == 0
        listed = CustomerActivityService(db=db).list_tasks(customer_id=customer.id)
        assert [(item.name, item.reminder_channel, item.reminder_minutes_before) for item in listed] == [
            ("Angebot nachfassen", "email", 0)
        ]

        changed = CustomerActivityService(db=db).update_task(
            customer_id=customer.id,
            task_id=task.id,
            name="Angebot geprüft",
            status="completed",
            due_date="2026-09-07",
            due_time="10:30",
            reminder_channel="popup",
            reminder_minutes_before="5",
            description="Erledigt.",
        )
        assert changed.name == "Angebot geprüft"
        assert changed.due_at == datetime(2026, 9, 7, 8, 30)
        assert changed.reminder_channel == "popup"

        CustomerActivityService(db=db).delete_task(customer_id=customer.id, task_id=task.id)
        assert CustomerActivityService(db=db).list_tasks(customer_id=customer.id) == ()


def test_completing_tasks_and_calls_hides_them_from_customer_activities():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        user = HubUser(username="hub-admin", password_hash="hash", reminder_email="team@example.de")
        db.add_all([customer, user])
        db.flush()
        service = CustomerActivityService(db=db)
        task = service.schedule_task(
            customer_id=customer.id,
            actor=user.username,
            name="Angebot nachfassen",
            status="planned",
            due_date="2026-09-10",
            due_time="10:00",
            reminder_channel="email",
            reminder_minutes_before="15",
            description="",
        )
        call = service.schedule_call(
            customer_id=customer.id,
            actor=user.username,
            name="Kunde zurückrufen",
            status="planned",
            direction="outbound",
            start_date="2026-09-10",
            start_time="11:00",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["5"],
            description="",
        )

        completed_task = service.complete_task(customer_id=customer.id, task_id=task.id)
        completed_call = service.complete_call(customer_id=customer.id, call_id=call.id)

        assert completed_task.status == "completed"
        assert completed_call.status == "completed"
        assert service.list_tasks(customer_id=customer.id) == ()
        assert service.list_calls(customer_id=customer.id) == ()
        assert db.scalar(
            select(CustomerTaskEmailReminder.status).where(CustomerTaskEmailReminder.task_id == task.id)
        ) == "cancelled"


def test_meetings_are_completed_when_their_end_time_is_reached_and_remain_visible():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add(customer)
        db.flush()
        service = CustomerActivityService(db=db)
        meeting = service.schedule_meeting(
            customer_id=customer.id,
            actor="hub-admin",
            name="Projektstart",
            status="planned",
            start_date="2026-09-10",
            start_time="10:00",
            duration_minutes="60",
            reminder_channels=[],
            reminder_minutes_before=[],
            description="",
        )

        assert service.complete_elapsed_meetings(now=meeting.ends_at) == 1
        db.refresh(meeting)
        listed = service.list_meetings(customer_id=customer.id)

        assert meeting.status == "completed"
        assert [(item.name, item.status) for item in listed] == [("Projektstart", "completed")]


def test_schedule_update_and_delete_meeting_use_the_meeting_module():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add(customer)
        db.flush()
        service = CustomerActivityService(db=db)

        meeting = service.schedule_meeting(
            customer_id=customer.id,
            actor="hub-admin",
            name="Projektstart",
            status="planned",
            start_date="2026-09-06",
            start_time="09:00",
            duration_minutes="30",
            reminder_channels=["popup", "email"],
            reminder_minutes_before=["5", "0"],
            description="Kickoff.",
        )
        assert meeting.starts_at == datetime(2026, 9, 6, 7, 0)
        assert meeting.ends_at == datetime(2026, 9, 6, 7, 30)
        assert [(reminder.channel, reminder.minutes_before) for reminder in meeting.reminders] == [("popup", 5), ("email", 0)]

        changed = service.update_meeting(
            customer_id=customer.id,
            meeting_id=meeting.id,
            name="Projektstart verschoben",
            status="completed",
            start_date="2026-09-07",
            start_time="10:00",
            duration_minutes="60",
            reminder_channels=["email", "popup"],
            reminder_minutes_before=["0", "15"],
            description="Erledigt.",
        )
        assert changed.name == "Projektstart verschoben"
        assert changed.starts_at == datetime(2026, 9, 7, 8, 0)
        assert changed.ends_at == datetime(2026, 9, 7, 9, 0)
        listed = CustomerActivityService(db=db).list_meetings(customer_id=customer.id)[0]
        assert listed.duration_minutes == 60
        assert listed.end_date == "2026-09-07"
        assert listed.end_time == "11:00"
        assert [(reminder.channel, reminder.minutes_before) for reminder in listed.reminders] == [("email", 0), ("popup", 15)]

        service.delete_meeting(customer_id=customer.id, meeting_id=meeting.id)
        assert service.list_meetings(customer_id=customer.id) == ()


def test_calendar_lists_calls_and_meetings_for_the_selected_week_only():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Stop & Shop", is_visible=True)
        db.add(customer)
        db.flush()
        service = CustomerActivityService(db=db)
        service.schedule_call(
            customer_id=customer.id,
            actor="hub-admin",
            name="Rückruf",
            status="planned",
            direction="outbound",
            start_date="2026-09-07",
            start_time="10:30",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["5"],
            description="",
        )
        service.schedule_meeting(
            customer_id=customer.id,
            actor="hub-admin",
            name="Projektstart",
            status="planned",
            start_date="2026-09-09",
            start_time="14:00",
            duration_minutes="60",
            reminder_channels=["popup"],
            reminder_minutes_before=["15"],
            description="",
        )
        service.schedule_meeting(
            customer_id=customer.id,
            actor="hub-admin",
            name="Nächste Woche",
            status="planned",
            start_date="2026-09-14",
            start_time="09:00",
            duration_minutes="60",
            reminder_channels=["popup"],
            reminder_minutes_before=["15"],
            description="",
        )

        activities = service.list_calendar_activities(week_start=date(2026, 9, 7))

        assert [(activity.kind, activity.name, activity.start_date, activity.start_time) for activity in activities] == [
            ("call", "Rückruf", "2026-09-07", "10:30"),
            ("meeting", "Projektstart", "2026-09-09", "14:00"),
        ]
        assert activities[1].end_time == "15:00"
        assert activities[0].customer_name == "Stop & Shop"
