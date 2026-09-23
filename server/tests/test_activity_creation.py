from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.record_actor import record_actor_scope
from app.core.templates import create_templates
from app.core.timezones import format_berlin_time_local
from app.db.base import Base
from app.db.record_info_tracking import install_record_info_tracking
from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerTaskActivity, CustomerMeetingActivity
from app.models.hub_user import HubUser
from app.services.customer_activities import CustomerActivityService, activity_creation_metadata


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    install_record_info_tracking(factory)
    with factory() as session:
        yield session
    engine.dispose()


def make_activity(db, kind, actor="short.login"):
    customer = Customer(name="Example")
    db.add(customer)
    db.flush()
    values = dict(customer_id=customer.id, name="Activity", status="planned", created_by_username=actor)
    scheduled = datetime(2030, 1, 7, 10, 0)
    if kind == "task":
        activity = CustomerTaskActivity(due_at=scheduled, **values)
    else:
        model = CustomerCallActivity if kind == "call" else CustomerMeetingActivity
        activity = model(starts_at=scheduled, ends_at=scheduled + timedelta(minutes=30), **values)
    db.add(activity)
    db.flush()
    return activity


@pytest.mark.parametrize("kind", ["call", "task", "meeting"])
@pytest.mark.parametrize("origin", ["ui", "agent"])
def test_creation_shared_by_all_views_and_unchanged_on_edit(db, kind, origin):
    author = HubUser(username="short.login", first_name="Sarah", last_name="Muster", password_hash="x", role="admin")
    editor = HubUser(username="other", first_name="Other", last_name="Employee", password_hash="x", role="admin")
    db.add_all([author, editor])
    db.commit()
    with record_actor_scope(db, author.username, origin=origin):
        activity = make_activity(db, kind)
        db.commit()
    original = activity_creation_metadata(activity)
    assert original["created_by"].startswith("Sarah Muster")
    assert ("Hub-Agent" in original["created_by"]) == (origin == "agent")
    assert original["created_at"].year != 2030
    with record_actor_scope(db, editor.username):
        activity.description = "Changed later"
        db.commit()
    assert activity_creation_metadata(activity) == original
    service = CustomerActivityService(db=db)
    entry = service.list_directory_entries(kind=kind)[0]
    plural = {"call": "calls", "task": "tasks", "meeting": "meetings"}[kind]
    views = [entry, getattr(service, "list_" + plural)(customer_id=activity.customer_id)[0]]
    if kind != "task":
        views += list(service.list_calendar_activities(week_start=date(2030, 1, 7), persist_elapsed=False))
    for view in views:
        assert view.created_by == original["created_by"]
        assert view.created_at == original["created_at"]
    templates = create_templates(directory="app/templates")
    macros = templates.env.get_template("partials/activity_creation.html").module
    html = macros.attributes(entry) + macros.summary(entry) + macros.fields(entry.created_by, entry.created_at)
    assert "Sarah Muster" in html
    assert format_berlin_time_local(original["created_at"]) in html
    assert "CET" not in html and "CEST" not in html


@pytest.mark.parametrize("kind", ["call", "task", "meeting"])
def test_historical_creation_uses_stored_values_not_current_employee(db, kind):
    activity = make_activity(db, kind, actor="Original Employee")
    # Simulate a record from before metadata tracking was introduced.
    from app.models.hub_record_info import HubRecordInfo
    from sqlalchemy import delete
    db.execute(delete(HubRecordInfo).where(HubRecordInfo.record_table == activity.__tablename__, HubRecordInfo.record_id == activity.id))
    activity.created_at = datetime(2025, 7, 28, 9, 30, 41)
    db.commit()
    info = activity_creation_metadata(activity)
    assert info == {"created_by": "Original Employee", "created_at": datetime(2025, 7, 28, 9, 30, 41)}


def test_creation_values_are_display_only_and_used_by_all_openers():
    composer = Path("app/templates/partials/customer_activity_composer.html").read_text(encoding="utf-8")
    block = composer.split('data-activity-creation-fields', 1)[1].split('</dl>', 1)[0]
    assert "<input" not in block and "<select" not in block
    assert "Erstellt von" in block and "Erstellt am" in block
    for filename in ("activity_directory.html", "customer_detail.html", "calendar.html", "partials/lead_activity_panel.html"):
        assert "creation_attributes(" in Path("app/templates", filename).read_text(encoding="utf-8")
    standalone = Path("app/templates/customer_activity_standalone.html").read_text(encoding="utf-8")
    assert "creation_fields(" in standalone


def test_creation_markup_escapes_employee_name():
    macros = create_templates(directory="app/templates").env.get_template("partials/activity_creation.html").module
    assert "&lt;script&gt;" in macros.fields("<script>", None)
