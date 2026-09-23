import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.api.routes import web
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity import CustomerTaskActivity
from app.models.hub_user import HubUser
from app.services.customer_activities import CustomerActivityService
from app.services.hub_leads import HubLeadService


def test_activity_directories_include_customer_lead_and_unlinked_entries():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Test-Kunde", is_visible=True)
        db.add(customer)
        lead = HubLeadService(db=db, cipher=get_secret_cipher()).create_lead(
            submitted_values={
                "lead_field__first_name": "Lena",
                "lead_field__last_name": "Leitner",
                "lead_field__lead_status": "Lead erstellt",
            }
        )
        db.flush()
        service = CustomerActivityService(db=db)
        call = service.schedule_call(
            customer_id=customer.id,
            actor="hub-admin",
            name="Kundenrückruf",
            status="planned",
            direction="outbound",
            start_date="2026-09-18",
            start_time="09:00",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["5"],
            description="Rückruf mit Unterlagen",
        )
        task = service.schedule_task(
            customer_id=None,
            actor="hub-admin",
            name="Interne Aufgabe",
            status="planned",
            due_date="2026-09-18",
            due_time="10:00",
            reminder_channel="popup",
            reminder_minutes_before="0",
            description="Ohne Bezug erledigen",
        )
        meeting = service.schedule_meeting(
            customer_id=None,
            lead_id=lead.id,
            actor="hub-admin",
            name="Lead-Gespräch",
            status="planned",
            start_date="2026-09-18",
            start_time="11:00",
            duration_minutes="60",
            reminder_channels=["popup"],
            reminder_minutes_before=["15"],
            description="Angebot besprechen",
        )

        call_entry = service.list_directory_entries(kind="call")[0]
        task_entry = service.list_directory_entries(kind="task")[0]
        meeting_entry = service.list_directory_entries(kind="meeting")[0]

        assert (call_entry.id, call_entry.related_kind, call_entry.related_name) == (call.id, "Kunde", "Test-Kunde")
        assert call_entry.direction_label == "Ausgehend"
        assert call_entry.duration_minutes == 30
        assert call_entry.scheduled_date == "2026-09-18"
        assert call_entry.scheduled_time == "09:00"
        assert call_entry.reminder_channels == ("popup",)
        assert call_entry.reminder_minutes_before == (5,)
        assert call_entry.description == "Rückruf mit Unterlagen"
        assert call_entry.relation_label == "Kunde · Test-Kunde"
        assert call_entry.update_href == f"/activities/call/{call.id}"
        assert (task_entry.id, task_entry.related_href) == (task.id, "")
        assert task_entry.reminder_label == "Popup · zum Termin"
        assert task_entry.relation_label == "Keine Verknüpfung"
        assert (meeting_entry.id, meeting_entry.related_kind, meeting_entry.related_name) == (
            meeting.id,
            "Lead",
            "Lena Leitner",
        )
        assert meeting_entry.duration_minutes == 60
        assert all(entry.status_label == "Geplant" for entry in (call_entry, task_entry, meeting_entry))
        assert all(
            entry.scheduled_at == datetime(2026, 9, 18, hour)
            for entry, hour in ((call_entry, 7), (task_entry, 8), (meeting_entry, 9))
        )

        service.update_directory_call(
            call_id=call.id,
            name="Kundenrückruf aktualisiert",
            status="completed",
            direction="outbound",
            start_date="2026-09-19",
            start_time="09:30",
            duration_minutes="45",
            reminder_channels=["email"],
            reminder_minutes_before=["15"],
            description="Aktualisiert",
        )
        service.update_directory_task(
            task_id=task.id,
            name="Interne Aufgabe aktualisiert",
            status="planned",
            due_date="2026-09-19",
            due_time="10:30",
            reminder_channel="popup",
            reminder_minutes_before="5",
            description="Aktualisiert",
        )
        service.update_directory_meeting(
            meeting_id=meeting.id,
            name="Lead-Gespräch aktualisiert",
            status="planned",
            start_date="2026-09-19",
            start_time="11:30",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["5"],
            description="Aktualisiert",
        )

        assert call.customer_id == customer.id
        assert call.lead_id is None
        assert task.customer_id is None
        assert task.lead_id is None
        assert task.case_id is None
        assert meeting.customer_id is None
        assert meeting.lead_id == lead.id


def test_activity_directory_template_and_navigation_are_wired():
    template = Path("app/templates/activity_directory.html").read_text(encoding="utf-8")
    navigation = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert 'href="{{ create_href }}"' in template
    assert "Keine Verknüpfung" in template
    assert "data-customer-call-edit" in template
    assert "data-customer-task-edit" in template
    assert "data-customer-meeting-edit" in template
    assert 'activity_composer_mode = "directory"' in template
    assert 'href="/calls"' in navigation
    assert 'href="/tasks"' in navigation
    assert 'href="/meetings"' in navigation
    route_paths = {route.path for route in web.router.routes}
    assert {"/calls", "/tasks", "/meetings"}.issubset(route_paths)
    assert any(
        route.path == "/activities/{activity_kind}/{activity_id}" and "POST" in route.methods
        for route in web.router.routes
    )
    web.templates.env.get_template("activity_directory.html")


def test_activity_directory_routes_render_their_own_tables():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(user)
        db.flush()
        pages = (
            ("/calls", web.calls_page, "Anrufe", "Anruf neu anlegen", "call"),
            ("/tasks", web.tasks_page, "Aufgaben", "Aufgabe neu anlegen", "task"),
            ("/meetings", web.meetings_page, "Meetings", "Meeting neu anlegen", "meeting"),
        )
        for path, route, title, create_label, create_kind in pages:
            scope = {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("test", 80),
                "client": ("test", 1),
                "session": {},
            }
            request = Request(scope)
            request.state.hub_user = user

            html = route(request, db).body.decode("utf-8")

            assert f"<h2>{title}</h2>" in html
            assert f'href="/calendar?create={create_kind}"' in html
            assert create_label in html
            assert 'class="finance-directory-table activity-directory-table"' in html
            assert 'data-activity-composer-mode="directory"' in html


def test_activity_directory_name_opens_the_matching_drawer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(user)
        db.flush()
        task = CustomerActivityService(db=db).schedule_task(
            customer_id=None,
            actor=user.username,
            name="Direkt bearbeitbare Aufgabe",
            status="planned",
            due_date="2026-09-18",
            due_time="10:00",
            reminder_channel="popup",
            reminder_minutes_before="5",
            description="Im rechten Panel öffnen",
        )
        db.add(user)
        db.flush()
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/tasks",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
            "session": {},
        }
        request = Request(scope)
        request.state.hub_user = user

        html = web.tasks_page(request, db).body.decode("utf-8")

        assert "Direkt bearbeitbare Aufgabe" in html
        assert "data-customer-task-edit" in html
        assert f'data-customer-task-update-url="/activities/task/{task.id}"' in html
        assert 'data-customer-task-due-date="2026-09-18"' in html
        assert 'data-customer-task-due-time="10:00"' in html
        assert 'data-customer-activity-relation="Keine Verknüpfung"' in html
        assert f'href="/activities/task/{task.id}"' not in html


def test_activity_directory_post_updates_in_place_and_returns_to_its_table(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web, "require_csrf", lambda request, token: None)
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: SimpleNamespace(username="hub-admin"))
    monkeypatch.setattr(web, "write_audit_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(web.TaskEmailReminderWorker, "notify_schedule_changed", lambda: None)

    class UpdateRequest:
        async def form(self):
            return FormData(
                [
                    ("name", "Aufgabe im Panel bearbeitet"),
                    ("status", "completed"),
                    ("due_date", "2026-09-20"),
                    ("due_time", "12:30"),
                    ("reminder_channel", "popup"),
                    ("reminder_minutes_before", "5"),
                    ("description", "Direkt gespeichert"),
                ]
            )

    with Session(engine) as db:
        customer = Customer(name="Test-Kunde", is_visible=True)
        db.add(customer)
        db.add(HubUser(username="hub-admin", password_hash="hash", role="admin"))
        db.flush()
        task = CustomerActivityService(db=db).schedule_task(
            customer_id=customer.id,
            actor="hub-admin",
            name="Vorher",
            status="planned",
            due_date="2026-09-18",
            due_time="10:00",
            reminder_channel="popup",
            reminder_minutes_before="5",
            description="",
        )

        response = asyncio.run(web.update_activity_from_directory("task", task.id, UpdateRequest(), db))

        assert response.status_code == 303
        assert response.headers["location"].startswith("/tasks?activity=success")
        assert task.name == "Aufgabe im Panel bearbeitet"
        assert task.status == "completed"
        assert task.customer_id == customer.id


def test_completed_task_from_directory_opens_its_standalone_detail():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Test-Kunde", is_visible=True)
        task = CustomerTaskActivity(
            customer=customer,
            name="Erledigte Aufgabe",
            status="completed",
            due_at=datetime(2026, 9, 18, 8),
        )
        actor = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add_all([task, actor])
        db.flush()
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/activities/task/{task.id}",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
            "session": {},
        }
        request = Request(scope)
        request.state.hub_user = actor

        response = web.customer_activity_permalink("task", task.id, request, db)

        assert response.status_code == 200
        assert "Erledigte Aufgabe" in response.body.decode("utf-8")
