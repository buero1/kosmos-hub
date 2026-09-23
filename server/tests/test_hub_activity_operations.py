import ast
import asyncio
import inspect
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.api.routes import web
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_agent import HubAgentAction, HubAgentConversation, HubAgentJob
from app.models.hub_user import HubUser
from app.services import hub_activity_catalog
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_activity_catalog import ACTIVITY_KINDS, activity_defaults, activity_fields, activity_form_defaults
from app.services.hub_agent import HubAgentError, HubAgentService
from app.services.hub_leads import HubLeadService
from app.services.hub_operation_activities import ACTIVITY_MODELS
from app.services.hub_operations import HubOperationError, HubOperationService, agent_operations, get_operation
from app.services.task_email_reminder_worker import TaskEmailReminderWorker


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = get_secret_cipher()
    wakes = []
    monkeypatch.setattr(TaskEmailReminderWorker, "notify_schedule_changed", lambda: wakes.append("wake"))
    monkeypatch.setattr(web, "require_csrf", lambda request, token: None)
    monkeypatch.setattr(web, "write_audit_log", lambda *args, **kwargs: None)
    with Session(engine) as db:
        user = HubUser(username="admin", password_hash="x", role="admin", reminder_email="admin@example.test")
        customer = Customer(name="Customer", is_visible=True)
        db.add_all([user, customer])
        lead = HubLeadService(db=db, cipher=cipher).create_lead(submitted_values={
            "lead_field__first_name": "Lena", "lead_field__last_name": "Test", "lead_field__company": "Test Company",
        })
        db.commit()
        monkeypatch.setattr(web, "_require_hub_admin", lambda request: user)
        yield SimpleNamespace(db=db, cipher=cipher, user=user, customer=customer, lead=lead,
                              service=HubOperationService(db=db, cipher=cipher, actor=user.username), wakes=wakes)
    engine.dispose()


def fields(kind):
    values = {"name": "Follow up", "status": "planned", "description": "Keep this description"}
    if kind == "task":
        values.update(due_date="2030-10-15", due_time="09:00", reminder_channel="popup", reminder_minutes_before="0")
    else:
        values.update(start_date="2030-10-15", start_time="09:00", duration_minutes="30",
                      reminder_channels="popup,email", reminder_minutes_before="5,15")
        if kind == "call":
            values["direction"] = "outbound"
    return values


def key(kind, action):
    return f"activities.{ACTIVITY_KINDS[kind][0]}.{action}"


def form(kind):
    pairs = []
    for name, value in fields(kind).items():
        for part in value.split(",") if kind != "task" and name in {"reminder_channels", "reminder_minutes_before"} else [value]:
            pairs.append((name, part))
    return FormData(pairs)


def request_for(data):
    class Request:
        async def form(self):
            return data
    return Request()


def agent_action(env, operation_key, values, actor="admin"):
    job = HubAgentJob(created_by_username=actor, status="ready",
        encrypted_request_json=env.cipher.encrypt(json.dumps({"instruction": "Test"})),
        encrypted_plan_json=env.cipher.encrypt(json.dumps({"summary": "Test", "response": "Test"})))
    env.db.add(job)
    env.db.flush()
    action = HubAgentAction(job_id=job.id, sort_order=0, action_type=operation_key, status="proposed",
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"title": "Test", "details": "Test", "input": values})))
    env.db.add(action)
    env.db.flush()
    return action


@pytest.mark.parametrize("kind", ACTIVITY_KINDS)
@pytest.mark.parametrize("owner", ["customer", "lead", "none"])
def test_agent_and_human_use_identical_activity_creation(env, kind, owner):
    relation = {} if owner == "none" else {f"{owner}_id": str(getattr(env, owner).id)}
    values = {**fields(kind), **relation}
    recorded = []
    original = HubOperationService.execute
    from unittest.mock import patch
    def execute(service, op, data):
        recorded.append(op)
        return original(service, op, data)
    with patch.object(HubOperationService, "execute", execute):
        if owner == "lead":
            response = asyncio.run(web.create_lead_activity(env.lead.id, ACTIVITY_KINDS[kind][0], request_for(form(kind)), env.db))
        elif owner == "customer":
            args = dict(fields(kind))
            if kind != "task":
                args["reminder_channels"] = args["reminder_channels"].split(",")
                args["reminder_minutes_before"] = args["reminder_minutes_before"].split(",")
            response = getattr(web, f"schedule_customer_{kind}")(customer_id=env.customer.id, request=None, db=env.db, **args)
        else:
            args = dict(fields(kind))
            if kind == "task":
                args["task_reminder_minutes_before"] = args.pop("reminder_minutes_before")
            else:
                args["reminder_channels"] = args["reminder_channels"].split(",")
                args["reminder_minutes_before"] = args["reminder_minutes_before"].split(",")
            response = web.schedule_calendar_activity(request=None, db=env.db, activity_kind=kind, **args)
        assert response.status_code == 303
        assert "error" not in response.headers["location"]
        human = env.db.scalars(select(ACTIVITY_MODELS[kind])).one()
        action = agent_action(env, key(kind, "create"), values)
        view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor="admin")
        assert view.status == "completed", view.error
        agent = env.db.scalars(select(ACTIVITY_MODELS[kind]).where(ACTIVITY_MODELS[kind].id != human.id)).one()
        assert recorded == [key(kind, "create"), key(kind, "create")]
        for field in ("name", "status", "customer_id", "lead_id", "description"):
            assert getattr(human, field) == getattr(agent, field)
        timestamp = "due_at" if kind == "task" else "starts_at"
        assert getattr(human, timestamp) == getattr(agent, timestamp) == datetime(2030, 10, 15, 7)
        if kind != "task":
            assert [(r.channel, r.minutes_before) for r in human.reminders] == [(r.channel, r.minutes_before) for r in agent.reminders]


@pytest.mark.parametrize("kind", ACTIVITY_KINDS)
def test_partial_update_preserves_values_and_can_clear_fields(env, kind):
    created = env.service.execute(key(kind, "create"), {**fields(kind), "lead_id": str(env.lead.id)})
    activity = env.db.get(ACTIVITY_MODELS[kind], created.record_id)
    before = activity.due_at if kind == "task" else activity.starts_at
    env.service.execute(key(kind, "update"), {"activity_id": str(activity.id), "name": "Renamed"})
    assert activity.description == "Keep this description"
    assert (activity.due_at if kind == "task" else activity.starts_at) == before
    assert activity.lead_id == env.lead.id
    reminder_field = "reminder_channel" if kind == "task" else "reminder_channels"
    env.service.execute(key(kind, "update"), {"activity_id": str(activity.id), "description": "", reminder_field: "none"})
    assert activity.description is None
    assert activity.reminder_channel == "none" if kind == "task" else activity.reminders == []
    if kind == "meeting":
        env.service.execute(key(kind, "update"), {"activity_id": str(activity.id), "status": "completed"})
    else:
        env.service.execute(key(kind, "complete"), {"activity_id": str(activity.id)})
    assert activity.status == "completed"
    env.service.execute(key(kind, "delete"), {"activity_id": str(activity.id)})
    assert env.db.get(ACTIVITY_MODELS[kind], activity.id) is None


@pytest.mark.parametrize("kind", ACTIVITY_KINDS)
def test_directory_edits_preserve_lead_and_use_gateway(env, kind):
    created = env.service.execute(key(kind, "create"), {**fields(kind), "lead_id": str(env.lead.id)})
    data = form(kind)
    response = asyncio.run(web.update_activity_from_directory(kind, created.record_id, request_for(data), env.db))
    assert "activity=success" in response.headers["location"]
    activity = env.db.get(ACTIVITY_MODELS[kind], created.record_id)
    assert activity.lead_id == env.lead.id
    assert activity.customer_id is None


@pytest.mark.parametrize("kind", ["call", "meeting"])
def test_calendar_relink_is_exclusive_and_authorized(env, kind):
    created = env.service.execute(key(kind, "create"), {**fields(kind), "lead_id": str(env.lead.id)})
    args = fields(kind)
    args["reminder_channels"] = args["reminder_channels"].split(",")
    args["reminder_minutes_before"] = args["reminder_minutes_before"].split(",")
    response = web.update_calendar_activity(kind, created.record_id, request=None, db=env.db,
        customer_id=str(env.customer.id), **args)
    assert "calendar=success" in response.headers["location"]
    activity = env.db.get(ACTIVITY_MODELS[kind], created.record_id)
    assert activity.customer_id == env.customer.id
    assert activity.lead_id is None


def calendar_args(kind):
    values = fields(kind)
    if kind == "task":
        values["task_reminder_minutes_before"] = values.pop("reminder_minutes_before")
    else:
        for name in ("reminder_channels", "reminder_minutes_before"):
            values[name] = values[name].split(",")
    return values


@pytest.mark.parametrize("kind", ACTIVITY_KINDS)
@pytest.mark.parametrize("owner", ["customer", "lead", "none"])
def test_calendar_search_selection_creates_exactly_one_owner(env, kind, owner):
    relation = {} if owner == "none" else {f"{owner}_id": str(getattr(env, owner).id)}
    response = web.schedule_calendar_activity(request=None, db=env.db, activity_kind=kind, **relation, **calendar_args(kind))
    assert "error" not in response.headers["location"]
    activity = env.db.scalars(select(ACTIVITY_MODELS[kind])).one()
    assert activity.customer_id == (env.customer.id if owner == "customer" else None)
    assert activity.lead_id == (env.lead.id if owner == "lead" else None)
    if kind == "task" and owner == "lead":
        assert response.headers["location"] == f"/leads/{env.lead.id}#lead-activities"


@pytest.mark.parametrize("kind", ACTIVITY_KINDS)
@pytest.mark.parametrize("relation", [{"lead_id": "bad"}, {"lead_id": "-1"}, {"lead_id": "99999"}, {"customer_id": "1", "lead_id": "1"}])
def test_calendar_rejects_invalid_or_ambiguous_owner_without_creating(env, kind, relation):
    response = web.schedule_calendar_activity(request=None, db=env.db, activity_kind=kind, **relation, **calendar_args(kind))
    assert "calendar=error" in response.headers["location"]
    assert not env.db.scalars(select(ACTIVITY_MODELS[kind])).all()


@pytest.mark.parametrize("kind", ["call", "meeting"])
def test_calendar_preserves_changes_and_clears_lead_from_search(env, kind):
    created = env.service.execute(key(kind, "create"), {**fields(kind), "customer_id": str(env.customer.id)})
    env.db.commit()
    for relation in ({"lead_id": str(env.lead.id)}, {"lead_id": str(env.lead.id)}, {"customer_id": "", "lead_id": ""}):
        response = web.update_calendar_activity(kind, created.record_id, request=None, db=env.db, **relation, **calendar_args(kind))
        assert "calendar=success" in response.headers["location"]
        activity = env.db.get(ACTIVITY_MODELS[kind], created.record_id)
        assert activity.lead_id == (env.lead.id if relation.get("lead_id") else None)
        assert activity.customer_id is None
        assert activity.name == fields(kind)["name"]


@pytest.mark.parametrize("kind", ACTIVITY_KINDS)
def test_calendar_cannot_link_a_hidden_lead(env, kind):
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    env.user.role = "sales"
    env.db.commit()
    response = web.schedule_calendar_activity(request=None, db=env.db, activity_kind=kind, lead_id=str(env.lead.id), **calendar_args(kind))
    assert "calendar=error" in response.headers["location"]
    assert not env.db.scalars(select(ACTIVITY_MODELS[kind])).all()


@pytest.mark.parametrize("kind", ACTIVITY_KINDS)
@pytest.mark.parametrize("action", ["create", "update", "delete"])
def test_activity_operations_enforce_module_and_record_rights(env, kind, action):
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    sales = HubUser(username="sales", password_hash="x", role="sales", reminder_email="sales@example.test")
    viewer = HubUser(username="viewer", password_hash="x", role="viewer")
    inactive = HubUser(username="inactive", password_hash="x", role="admin", is_active=False)
    env.db.add_all([sales, viewer, inactive])
    env.db.flush()
    created = env.service.execute(key(kind, "create"), {**fields(kind), "lead_id": str(env.lead.id)})
    values = {**fields(kind), "lead_id": str(env.lead.id)} if action == "create" else {"activity_id": str(created.record_id)}
    for user in (sales, viewer, inactive):
        with pytest.raises(HubOperationError):
            HubOperationService(db=env.db, cipher=env.cipher, actor=user.username).execute(key(kind, action), values)
    access.assign_record(module_key="leads", record_id=env.lead.id, owner_user_id=sales.id, team_id=None)
    if action == "update":
        # CRM access alone no longer gives edit rights over somebody else's activity.
        with pytest.raises(HubOperationError):
            HubOperationService(db=env.db, cipher=env.cipher, actor=sales.username).execute(key(kind, action), values)
        env.service.execute(key(kind, "update"), {"activity_id": str(created.record_id), "assignee_user_id": str(sales.id)})
    if action != "delete":
        assert HubOperationService(db=env.db, cipher=env.cipher, actor=sales.username).execute(key(kind, action), values).record_id


@pytest.mark.parametrize("kind", ACTIVITY_KINDS)
def test_explicit_owner_must_match_activity_and_names_must_be_unambiguous(env, kind):
    first = env.service.execute(key(kind, "create"), {**fields(kind), "lead_id": str(env.lead.id)})
    with pytest.raises(HubOperationError):
        env.service.execute(key(kind, "delete"), {"activity_id": str(first.record_id), "customer_id": str(env.customer.id)})
    env.service.execute(key(kind, "create"), {**fields(kind), "lead_id": str(env.lead.id)})
    with pytest.raises(HubOperationError):
        env.service.execute(key(kind, "delete"), {"target_name": "Follow up", "lead_id": str(env.lead.id)})
    assert len(env.db.scalars(select(ACTIVITY_MODELS[kind])).all()) == 2


def test_failed_agent_update_rolls_back_relink_and_does_not_wake_worker(env):
    created = env.service.execute(key("call", "create"), {**fields("call"), "lead_id": str(env.lead.id)})
    env.db.commit()
    env.wakes.clear()  # Calls now also schedule their email reminders.
    action = agent_action(env, key("call", "update"), {"activity_id": str(created.record_id),
        "new_customer_id": str(env.customer.id), "duration_minutes": "9999"})
    view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor="admin")
    assert view.status == "failed"
    activity = env.db.get(ACTIVITY_MODELS["call"], created.record_id)
    assert activity.lead_id == env.lead.id
    assert activity.customer_id is None
    assert env.wakes == []


def test_defaults_and_required_fields_are_shared_with_drawer(env, monkeypatch):
    now = datetime(2030, 1, 31, 10, 25, tzinfo=ZoneInfo("Europe/Berlin"))
    defaults = activity_form_defaults(now=now)
    monkeypatch.setattr(hub_activity_catalog, "activity_form_defaults", lambda: defaults)
    assert defaults["task_defaults"]["due_date"] == "2030-02-01"
    assert defaults["call_defaults"]["start_time"] == "11:00"
    for kind in ACTIVITY_KINDS:
        operation = get_operation(key(kind, "create"))
        resolved = operation.apply_defaults({"name": "Default task"})
        assert resolved == {**activity_defaults(kind), "name": "Default task"}
        catalog = web.templates.env.globals["activity_field_catalog"](kind)
        for field in activity_fields(kind):
            assert operation.input_contract()[field.name]["required"] == catalog[field.name].required
        result = env.service.execute(key(kind, "create"), {"name": "Default task"})
        assert env.db.get(ACTIVITY_MODELS[kind], result.record_id).status == "planned"
    template = Path("app/templates/partials/customer_activity_composer.html").read_text(encoding="utf-8")
    for kind in ACTIVITY_KINDS:
        assert f"{kind}_fields.name.required" in template
    for name in ("lead_detail_page", "customer_detail_page", "_activity_directory_page", "calendar_page"):
        assert "activity_form_defaults" in inspect.getsource(getattr(web, name))


@pytest.mark.parametrize("outcome", ["commit", "rollback", "savepoint-commit", "savepoint-rollback", "outer-rollback"])
def test_worker_wakes_only_after_successful_outer_commit(env, outcome):
    def execute():
        env.service.execute(key("task", "create"), {**fields("task"), "reminder_channel": "email"})
    if outcome in {"savepoint-commit", "savepoint-rollback", "outer-rollback"}:
        savepoint = env.db.begin_nested()
        execute()
        if outcome == "savepoint-rollback":
            savepoint.rollback()
        else:
            savepoint.commit()
    else:
        execute()
    assert env.wakes == []
    if outcome in {"rollback", "outer-rollback"}:
        env.db.rollback()
    else:
        env.db.commit()
    assert env.wakes == (["wake"] if outcome in {"commit", "savepoint-commit"} else [])


def test_completed_and_deleted_task_cancel_email_reminders(env):
    created = env.service.execute(key("task", "create"), {**fields("task"), "reminder_channel": "email"})
    reminder = env.db.scalars(select(CustomerTaskEmailReminder)).one()
    assert reminder.status == "scheduled"
    env.service.execute(key("task", "complete"), {"activity_id": str(created.record_id)})
    assert reminder.status == "cancelled"
    env.service.execute(key("task", "delete"), {"activity_id": str(created.record_id)})
    assert reminder.task_id is None


@pytest.mark.parametrize("old_key", [
    "create_task", "update_task", "complete_task", "delete_task",
    "schedule_call", "update_call", "complete_call", "delete_call",
])
def test_obsolete_activity_actions_are_not_advertised_or_executable(env, old_key):
    available = {operation.key for operation in agent_operations()}
    assert old_key not in available
    assert get_operation(old_key) is None
    assert "activities.tasks.create" in available
    values = {"customer_name": env.customer.name, "task_name": "Legacy task", "call_name": "Legacy call",
        "due_date": "2030-10-15", "due_time": "09:00", "start_date": "2030-10-15", "start_time": "09:00",
        "duration_minutes": "30", "reminder_channel": "none"}
    with pytest.raises(HubOperationError):
        env.service.execute(old_key, values)
    with pytest.raises(HubAgentError):
        HubAgentService._normalize_action({"action_type": old_key, "input": values})
    action = agent_action(env, old_key, values)
    result = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor="admin")
    assert result.status == "failed"
    assert result.error
    for model in ACTIVITY_MODELS.values():
        assert env.db.scalars(select(model)).all() == []
    assert env.db.scalars(select(CustomerTaskEmailReminder)).all() == []
    assert env.wakes == []
    assert env.db.get(HubAgentJob, action.job_id) is not None
    assert env.db.get(HubAgentAction, action.id) is not None


def test_activity_write_routes_cannot_bypass_gateway():
    tree = ast.parse(Path("app/api/routes/web.py").read_text(encoding="utf-8"))
    checked = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        if (name.endswith(("_customer_call", "_customer_task", "_customer_meeting", "_lead_activity", "_calendar_activity"))
            and name.startswith(("schedule_", "create_", "update_", "complete_", "delete_"))) or name == "update_activity_from_directory":
            calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
            assert any(isinstance(call.func, ast.Name) and call.func.id == "_execute_activity_operation" for call in calls), name
            assert not any(isinstance(call.func, ast.Name) and call.func.id in {"CustomerActivityService", "LeadActivityService"} for call in calls), name
            checked.append(name)
    assert len(checked) == 18


def test_result_ids_can_feed_next_steps_and_followup_messages(env):
    conversation = HubAgentConversation(created_by_username="admin", status="active", encrypted_title_json=env.cipher.encrypt('{"title":"Test"}'))
    env.db.add(conversation)
    first = agent_action(env, key("meeting", "create"), {**fields("meeting"), "lead_id": str(env.lead.id)})
    first.job.conversation = conversation
    service = HubAgentService(db=env.db, cipher=env.cipher)
    result = service.execute_action(action_id=first.id, actor="admin")
    assert result.status == "completed"
    stored = json.loads(env.cipher.decrypt(first.encrypted_result_json))
    second = HubAgentAction(job=first.job, sort_order=1, action_type=key("meeting", "update"), status="proposed",
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"title": "Change meeting", "details": "Test", "input": {
            "activity_id": "{{action.1.activity_id}}", "start_time": "11:30",
        }})))
    env.db.add(second)
    env.db.flush()
    assert service.execute_action(action_id=second.id, actor="admin").status == "completed"
    meeting = env.db.get(ACTIVITY_MODELS["meeting"], int(stored["record_id"]))
    assert meeting.starts_at == datetime(2030, 10, 15, 9, 30)
    history = "\n".join(service._conversation_history(conversation))
    assert '"activity_id": "' + str(meeting.id) + '"' in history
    assert '"lead_id": "' + str(env.lead.id) + '"' in history
    assert "activity_id" in get_operation(key("meeting", "create")).agent_description()


def test_relink_cannot_reach_an_unassigned_lead(env):
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    sales = HubUser(username="sales", password_hash="x", role="sales")
    env.db.add(sales)
    env.db.flush()
    access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=sales.id, team_id=None)
    result = env.service.execute(key("meeting", "create"), {**fields("meeting"), "customer_id": str(env.customer.id)})
    service = HubOperationService(db=env.db, cipher=env.cipher, actor="sales")
    with pytest.raises(HubOperationError):
        service.execute(key("meeting", "update"), {"activity_id": str(result.record_id), "new_lead_id": str(env.lead.id)})
    meeting = env.db.get(ACTIVITY_MODELS["meeting"], result.record_id)
    assert meeting.customer_id == env.customer.id
    assert meeting.lead_id is None


def test_task_reminder_wakes_once_for_multiple_actions_and_ignores_rolled_back_child(env):
    values = {**fields("task"), "reminder_channel": "email"}
    env.service.execute(key("task", "create"), values)
    child = env.db.begin_nested()
    env.service.execute(key("task", "create"), values)
    child.rollback()
    env.service.execute(key("task", "create"), values)
    env.db.commit()
    assert env.wakes == ["wake"]
    assert len(env.db.scalars(select(CustomerTaskEmailReminder)).all()) == 2


@pytest.mark.parametrize("kind", ["call", "task", "meeting"])
@pytest.mark.parametrize("owner", ["customer", "lead", "none"])
def test_activity_directory_supplies_related_record_link(env, kind, owner):
    from starlette.requests import Request

    values = fields(kind)
    if owner != "none":
        values[owner + "_id"] = str(getattr(env, owner).id)
    env.service.execute(key(kind, "create"), values)
    env.db.commit()
    request = Request({"type": "http", "method": "GET", "path": "/calls", "headers": [],
                       "query_string": b"", "session": {}})
    request.state.hub_user = env.user
    html = web._activity_directory_page(request=request, db=env.db, kind=kind).body.decode()
    href = "" if owner == "none" else f"/{owner}s/{getattr(env, owner).id}"
    assert f'data-customer-activity-relation-href="{href}"' in html
    assert '<a data-customer-activity-directory-relation hidden></a>' in html
    assert '<input value="Keine Verkn' not in html
