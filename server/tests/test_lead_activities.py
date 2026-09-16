import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.api.routes import web
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.services.customer_activities import CustomerActivityError, CustomerActivityService
from app.services.customer_desktop_reminders import CustomerDesktopReminderService
from app.services.hub_leads import HubLeadService
from app.services.lead_activities import LeadActivityService
from app.services.styling_settings import StylingRuntimeSettings


def _lead(db, name):
    return HubLeadService(db=db, cipher=get_secret_cipher()).create_lead(
        submitted_values={"lead_field__first_name": name, "lead_field__last_name": "Test", "lead_field__lead_status": "Lead erstellt"}
    )


def test_lead_calls_are_separate_from_customer_calls_and_link_back_to_lead(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 14, 10, 0)
    monkeypatch.setattr(CustomerDesktopReminderService, "_utc_now", staticmethod(lambda: now))

    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        customer = Customer(name="Kunde", is_visible=True)
        db.add_all([user, customer])
        lead = _lead(db, "Erika")
        other_lead = _lead(db, "Nora")
        db.flush()
        activities = CustomerActivityService(db=db)
        call = activities.schedule_call(
            customer_id=None,
            lead_id=lead.id,
            actor=user.username,
            name="Erika anrufen",
            status="planned",
            direction="outbound",
            start_date="2026-09-14",
            start_time="12:00",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["0"],
            description="",
        )

        assert activities.list_calls(lead_id=lead.id)[0].id == call.id
        assert activities.list_calls(lead_id=other_lead.id) == ()
        assert activities.list_calls(customer_id=customer.id) == ()
        calendar_event = next(event for event in activities.list_calendar_activities(week_start=date(2026, 9, 14)) if event.id == call.id and event.kind == "call")
        assert calendar_event.lead_id == lead.id
        assert calendar_event.lead_name == "Erika Test"
        assert web.customer_activity_permalink("call", call.id, None, db).headers["location"] == f"/leads/{lead.id}#call-{call.id}"
        with pytest.raises(CustomerActivityError, match="nicht gefunden"):
            LeadActivityService(db=db).delete(lead_id=other_lead.id, kind="call", activity_id=call.id)

        due = CustomerDesktopReminderService(db=db).list_due_reminders(user=user)
        assert due[0].as_dict()["related_label"] == "Lead"
        assert due[0].as_dict()["related_url"] == f"/leads/{lead.id}"
        assert due[0].as_dict()["activity_url"] == f"/activities/call/{call.id}"


def test_lead_task_and_meeting_can_be_created_and_updated():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        lead = _lead(db, "Erika")
        db.flush()
        activities = CustomerActivityService(db=db)
        task = activities.schedule_task(
            customer_id=None,
            lead_id=lead.id,
            actor="hub-admin",
            name="Nachfassen",
            status="planned",
            due_date="2026-09-15",
            due_time="09:00",
            reminder_channel="popup",
            reminder_minutes_before="0",
            description="",
        )
        meeting = activities.schedule_meeting(
            customer_id=None,
            lead_id=lead.id,
            actor="hub-admin",
            name="Erstgespräch",
            status="planned",
            start_date="2026-09-16",
            start_time="10:00",
            duration_minutes="60",
            reminder_channels=["popup"],
            reminder_minutes_before=["15"],
            description="",
        )
        assert activities.list_tasks(lead_id=lead.id)[0].id == task.id
        assert activities.list_meetings(lead_id=lead.id)[0].id == meeting.id

        updated = LeadActivityService(db=db).update_task(
            lead_id=lead.id,
            task_id=task.id,
            name="Nachfassen erneut",
            status="planned",
            due_date="2026-09-17",
            due_time="09:00",
            reminder_channel="popup",
            reminder_minutes_before="0",
            description="Anrufen",
        )
        assert updated.name == "Nachfassen erneut"
        assert updated.lead_id == lead.id
        assert LeadActivityService(db=db).complete(lead_id=lead.id, kind="task", activity_id=task.id).status == "completed"
        assert activities.list_tasks(lead_id=lead.id) == ()


def test_lead_activity_form_fields_and_right_column_template():
    call = web._lead_activity_fields(FormData([("name", "Anruf"), ("direction", "inbound"), ("reminder_channels", "popup"), ("reminder_minutes_before", "5")]), "calls")
    task = web._lead_activity_fields(FormData([("name", "Aufgabe"), ("due_date", "2026-09-15"), ("reminder_channel", "popup")]), "tasks")

    assert call["direction"] == "inbound"
    assert call["reminder_channels"] == ["popup"]
    assert task["due_date"] == "2026-09-15"
    assert task["reminder_channel"] == "popup"

    template = Path("app/templates/lead_detail.html").read_text(encoding="utf-8")
    assert template.index("{{ lead_notes_panel() }}") < template.index('"partials/lead_activity_panel.html"')
    assert template.index("<aside class=\"customer-detail-side-column\">") < template.index("{{ lead_notes_panel() }}")
    web.templates.env.get_template("lead_detail.html")
    web.templates.env.get_template("partials/lead_activity_panel.html")


def test_lead_activity_post_saves_the_lead_relation(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web, "require_csrf", lambda request, token: None)
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: SimpleNamespace(username="hub-admin"))
    monkeypatch.setattr(web, "write_audit_log", lambda *args, **kwargs: None)

    class Request:
        async def form(self):
            return FormData([
                ("name", "Lead anrufen"),
                ("status", "planned"),
                ("start_date", "2026-09-14"),
                ("start_time", "12:00"),
                ("duration_minutes", "30"),
                ("reminder_channels", "popup"),
                ("reminder_minutes_before", "5"),
            ])

    with Session(engine) as db:
        lead = _lead(db, "Erika")
        db.flush()
        response = asyncio.run(web.create_lead_activity(lead.id, "calls", Request(), db))
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"/leads/{lead.id}?activity=success")
        assert CustomerActivityService(db=db).list_calls(lead_id=lead.id)[0].name == "Lead anrufen"


def test_lead_detail_renders_notes_then_activity_card_on_the_right(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web.templates, "context_processors", [lambda request: {
        "styling": StylingRuntimeSettings(),
        "unread_email_count": 0,
        "can_launch_wordpress_admin": False,
        "can_use_global_email_composer": False,
        "email_composer_settings": None,
        "email_ai_prompt_presets": (),
        "agent_page_context": None,
    }])

    with Session(engine) as db:
        lead = _lead(db, "Erika")
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(user)
        db.flush()
        call = CustomerActivityService(db=db).schedule_call(
            customer_id=None,
            lead_id=lead.id,
            actor=user.username,
            name="Erika anrufen",
            status="planned",
            direction="outbound",
            start_date="2026-09-15",
            start_time="10:00",
            duration_minutes="30",
            reminder_channels=["popup"],
            reminder_minutes_before=["5"],
            description="Rückruf vereinbaren",
        )
        scope = {"type": "http", "method": "GET", "path": f"/leads/{lead.id}", "headers": [], "query_string": b"", "scheme": "http", "server": ("test", 80), "client": ("test", 1), "session": {}}
        request = Request(scope)
        request.state.hub_user = user

        html = web.lead_detail_page(lead.id, request, db).body.decode("utf-8")

        side_column = html.index('class="customer-detail-side-column"')
        assert side_column < html.index('id="lead-notes"') < html.index('id="lead-activities"')
        assert f'action="/leads/{lead.id}/notes"' in html
        assert "Eine Notiz hinzufügen" in html
        assert "data-customer-note-edit-dialog" in html
        assert 'class="customer-detail-main-column" data-layout-editor' not in html
        lead_fields_start = html.index('id="lead-fields"')
        lead_fields_end = html.index("</article>", lead_fields_start)
        lead_fields_panel = html[lead_fields_start:lead_fields_end]
        assert 'data-layout-editor="lead-fields"' in lead_fields_panel
        assert "data-layout-fields-editor" in lead_fields_panel
        assert "data-layout-sortable" in lead_fields_panel
        assert 'id="lead-fields-layout-form"' in lead_fields_panel
        assert f'action="/leads/{lead.id}/activities/calls"' in html
        assert 'data-customer-activity-action="task"' in html
        assert f'id="call-{call.id}"' in html
        assert f'data-customer-call-update-url="/leads/{lead.id}/activities/calls/{call.id}"' in html
