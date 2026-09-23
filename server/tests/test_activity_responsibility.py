from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_access_control import HubTeam
from app.models.hub_user import HubUser
from app.services.customer_desktop_reminders import CustomerDesktopReminderService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_activity_responsibility import ACTIVITY_MODELS, ActivityResponsibility
from app.services.hub_activity_responsibility_schema import ensure_activity_responsibility_schema
from app.services.hub_crm_readers import HubCrmReadService
from app.services.hub_operations import HubOperationError, HubOperationService
from app.services.task_email_reminders import TaskEmailReminderService
from app.services.task_email_reminder_worker import TaskEmailReminderWorker


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(TaskEmailReminderWorker, "notify_schedule_changed", lambda: None)
    with Session(engine) as db:
        access = HubAccessControlService(db=db)
        access.ensure_defaults()
        team, other_team = HubTeam(name="Studio"), HubTeam(name="Other")
        db.add_all([team, other_team])
        db.flush()
        users = {}
        for name, role, team_id in (("admin", "admin", None), ("manager", "management", team.id),
                ("steffi", "employee", team.id), ("colleague", "employee", team.id), ("other", "employee", other_team.id)):
            users[name] = HubUser(username=name, first_name=name.title(), password_hash="x", role=role,
                team_id=team_id, reminder_email=name + "@example.test")
        customer = Customer(name="Visible customer", is_visible=True)
        excluded = Customer(name="Excluded customer", is_visible=True)
        db.add_all([*users.values(), customer, excluded])
        db.flush()
        access.assign_record(module_key="customers", record_id=customer.id, owner_user_id=users["steffi"].id, team_id=team.id)
        db.commit()
        cipher = get_secret_cipher()
        yield SimpleNamespace(db=db, users=users, access=access, customer=customer, excluded=excluded, cipher=cipher,
            service=lambda name: HubOperationService(db=db, cipher=cipher, actor=name))
    engine.dispose()


def key(kind, action):
    return f"activities.{'calls' if kind == 'call' else 'tasks' if kind == 'task' else 'meetings'}.{action}"


def create(env, kind, *, actor="admin", owner=None, **extra):
    fields = {"name": "Ownership test", "reminder_channel": "none", "reminder_channels": "none",
              "start_date": "2030-10-15", "start_time": "09:00", "due_date": "2030-10-15", "due_time": "09:00"}
    if kind == "task":
        for field in ("start_date", "start_time", "reminder_channels"):
            fields.pop(field)
    else:
        for field in ("due_date", "due_time", "reminder_channel"):
            fields.pop(field)
    if owner:
        fields["assignee_user_id"] = str(env.users[owner].id)
    result = env.service(actor).execute(key(kind, "create"), {**fields, **extra})
    return env.db.get(ACTIVITY_MODELS[kind], result.record_id)


@pytest.mark.parametrize("kind", ACTIVITY_MODELS)
def test_defaults_creator_and_completion_are_independent(env, kind):
    row = create(env, kind, actor="steffi")
    assert row.created_by_user_id == row.assignee_user_id == env.users["steffi"].id
    env.service("manager").execute(key(kind, "update"), {"activity_id": str(row.id), "assignee_user_id": str(env.users["colleague"].id)})
    assert row.created_by_user_id == env.users["steffi"].id
    assert row.assignee_user_id == env.users["colleague"].id
    policy = ActivityResponsibility(env.db, env.users["steffi"])
    assert policy.matches(row, "created") and not policy.matches(row, "mine")
    assert policy.visible(row) and not policy.allowed(row, "edit")
    env.service("colleague").execute(key(kind, "update"), {"activity_id": str(row.id), "status": "completed"})
    assert row.completed_by_user_id == env.users["colleague"].id and row.completed_at
    assert row.completed_by_name == "Colleague"
    env.service("colleague").execute(key(kind, "update"), {"activity_id": str(row.id), "status": "planned"})
    assert row.completed_at is None and row.completed_by_user_id is None


@pytest.mark.parametrize("kind", ACTIVITY_MODELS)
def test_assignment_never_grants_crm_access_and_failure_is_atomic(env, kind):
    with pytest.raises(HubOperationError, match="Zuweisung erweitert"):
        create(env, kind, owner="steffi", customer_id=str(env.excluded.id))
    assert not list(env.db.scalars(select(ACTIVITY_MODELS[kind])))
    row = create(env, kind, owner="steffi", customer_id=str(env.customer.id))
    with pytest.raises(HubOperationError):
        env.service("admin").execute(key(kind, "update"), {"activity_id": str(row.id), "assignee_user_id": str(env.users["other"].id), "name": "MUST NOT SAVE"})
    env.db.commit()
    assert row.name == "Ownership test" and row.assignee_user_id == env.users["steffi"].id


@pytest.mark.parametrize("kind", ACTIVITY_MODELS)
def test_edit_read_delete_and_delegate_permissions(env, kind):
    row = create(env, kind, owner="steffi")
    for actor in ("colleague", "other"):
        for action in ("update", "delete"):
            with pytest.raises(HubOperationError):
                env.service(actor).execute(key(kind, action), {"activity_id": str(row.id)})
    with pytest.raises(HubOperationError):
        env.service("steffi").execute(key(kind, "update"), {"activity_id": str(row.id), "assignee_user_id": str(env.users["colleague"].id)})
    with pytest.raises(HubOperationError):
        env.service("manager").execute(key(kind, "update"), {"activity_id": str(row.id), "assignee_user_id": str(env.users["other"].id)})
    env.access.permission(role_key="management", module_key="activities").can_manage = False
    env.db.flush()
    assert ActivityResponsibility(env.db, env.users["manager"]).visible(row)
    with pytest.raises(HubOperationError, match="Verwalten"):
        env.service("manager").execute(key(kind, "update"), {"activity_id": str(row.id)})


@pytest.mark.parametrize("kind", ACTIVITY_MODELS)
def test_directory_filters_and_rename_keep_stable_assignment(env, kind):
    mine = create(env, kind, actor="steffi")
    create(env, kind, owner="colleague")
    create(env, kind, owner="other")
    env.users["steffi"].username = "renamed"
    env.db.flush()
    reader = HubCrmReadService(db=env.db, cipher=env.cipher, actor="renamed")
    assert [row.id for row in reader.activity_entries(kind, view="mine")] == [mine.id]
    team_reader = HubCrmReadService(db=env.db, cipher=env.cipher, actor="manager")
    assert len(team_reader.activity_entries(kind, view="team")) == 2
    assert len(team_reader.activity_entries(kind, view="all")) == 2


@pytest.mark.parametrize("kind", ACTIVITY_MODELS)
def test_email_and_popup_reminders_follow_assignee_and_recheck_access(env, kind, monkeypatch):
    channels = {"reminder_channel": "email", "reminder_minutes_before": "5"} if kind == "task" else {
        "reminder_channels": "email,popup", "reminder_minutes_before": "5,5"}
    row = create(env, kind, owner="steffi", **channels)
    job = env.db.scalars(select(CustomerTaskEmailReminder)).one()
    assert job.recipient_user_id == env.users["steffi"].id and job.recipient_email == "steffi@example.test"
    env.db.add(CustomerActivityReminderNotification(user_id=env.users["steffi"].id, activity_kind=kind,
        activity_id=row.id, reminder_key="0", remind_at=datetime(2030, 10, 15, 6, 55)))
    env.db.flush()
    env.service("manager").execute(key(kind, "update"), {"activity_id": str(row.id), "assignee_user_id": str(env.users["colleague"].id)})
    assert job.recipient_email == "colleague@example.test"
    assert not list(env.db.scalars(select(CustomerActivityReminderNotification)))
    now = datetime(2030, 10, 15, 7)
    monkeypatch.setattr(CustomerDesktopReminderService, "_utc_now", staticmethod(lambda: now))
    popup = CustomerDesktopReminderService(db=env.db)
    assert not popup.list_due_reminders(user=env.users["steffi"])
    if kind != "task":
        assert len(popup.list_due_reminders(user=env.users["colleague"])) == 1
    env.users["colleague"].is_active = False
    env.db.commit()
    monkeypatch.setattr(TaskEmailReminderService, "_utc_now", staticmethod(lambda: now))
    result = TaskEmailReminderService(db=env.db, cipher=env.cipher).process_due_reminders()
    assert result.cancelled == 1 and result.sent == 0


def test_inflight_delivery_blocks_reassignment_and_no_access_escalation(env):
    row = create(env, "task", owner="steffi", reminder_channel="email", reminder_minutes_before="0")
    job = env.db.scalars(select(CustomerTaskEmailReminder)).one()
    job.status = "sending"
    env.db.flush()
    with pytest.raises(HubOperationError, match="gerade versendet"):
        env.service("admin").execute(key("task", "update"), {"activity_id": str(row.id), "assignee_user_id": str(env.users["colleague"].id)})
    assert row.assignee_user_id == env.users["steffi"].id


def test_schema_backfill_is_once_only_and_does_not_invent_historical_owners():
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE hub_users (id INTEGER PRIMARY KEY, username VARCHAR(64), is_active BOOLEAN)"))
        db.execute(text("INSERT INTO hub_users VALUES (1, 'active', true), (2, 'inactive', false)"))
        db.execute(text("CREATE TABLE customer_task_activities (id INTEGER PRIMARY KEY, created_by_username VARCHAR(64))"))
        db.execute(text("INSERT INTO customer_task_activities VALUES (1, 'active'), (2, 'inactive'), (3, 'Imported Person')"))
    ensure_activity_responsibility_schema(engine)
    with engine.begin() as db:
        rows = db.execute(text("SELECT created_by_user_id, assignee_user_id FROM customer_task_activities ORDER BY id")).all()
        assert rows == [(1, 1), (2, None), (None, None)]
        db.execute(text("UPDATE customer_task_activities SET assignee_user_id = NULL WHERE id = 1"))
    ensure_activity_responsibility_schema(engine)
    with engine.connect() as db:
        assert db.scalar(text("SELECT assignee_user_id FROM customer_task_activities WHERE id = 1")) is None
    engine.dispose()


def test_search_and_existing_agent_context_do_not_reveal_foreign_activity(env):
    from app.services.hub_global_search import HubGlobalSearchService
    from app.services.hub_agent import HubAgentService, HubAgentError
    row = create(env, "call", owner="steffi")
    search = HubGlobalSearchService(db=env.db, cipher=env.cipher)
    assert search.search_for_user("Ownership", user=env.users["steffi"], module="activities")
    assert not search.search_for_user("Ownership", user=env.users["other"], module="activities")
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    chat = agent.add_context(actor="steffi", resource_type="call", resource_key=str(row.id))
    conversation = agent._conversation_for_actor(actor="steffi", conversation_id=chat.conversation_id, create_if_missing=False)
    assert "Ownership test" in "".join(agent._conversation_prompt_contexts(conversation, exclude_email_key=""))
    env.service("admin").execute(key("call", "update"), {"activity_id": str(row.id), "assignee_user_id": str(env.users["other"].id)})
    with pytest.raises(HubAgentError):
        agent.add_context(actor="steffi", resource_type="call", resource_key=str(row.id))
    assert "Ownership test" not in "".join(agent._conversation_prompt_contexts(conversation, exclude_email_key=""))


def test_migration_keeps_action_rights_and_does_not_reset_reminder_recipients():
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE hub_users (id INTEGER PRIMARY KEY, username VARCHAR(64), is_active BOOLEAN)"))
        db.execute(text("INSERT INTO hub_users VALUES (1, 'active', true)"))
        db.execute(text("CREATE TABLE customer_task_activities (id INTEGER PRIMARY KEY, created_by_username VARCHAR(64))"))
        db.execute(text("INSERT INTO customer_task_activities VALUES (1, 'active')"))
        db.execute(text("CREATE TABLE customer_task_email_reminders (id INTEGER PRIMARY KEY, task_id INTEGER)"))
        db.execute(text("INSERT INTO customer_task_email_reminders VALUES (1, 1)"))
        db.execute(text("CREATE TABLE hub_role_permissions (role_key VARCHAR(64), module_key VARCHAR(64), record_scope VARCHAR(16), can_manage BOOLEAN)"))
        db.execute(text("INSERT INTO hub_role_permissions VALUES ('management', 'activities', 'all', false), ('sales', 'activities', 'all', false), ('custom', 'activities', 'all', true)"))
    ensure_activity_responsibility_schema(engine)
    with engine.begin() as db:
        assert db.execute(text("SELECT activity_kind, activity_id, reminder_key, recipient_user_id FROM customer_task_email_reminders")).one() == ('task', 1, 'primary', 1)
        assert dict(db.execute(text("SELECT role_key, record_scope FROM hub_role_permissions")).all()) == {
            'management': 'team', 'sales': 'assigned', 'custom': 'all'}
        assert not db.scalar(text("SELECT can_manage FROM hub_role_permissions WHERE role_key = 'management'"))
        db.execute(text("UPDATE hub_role_permissions SET record_scope = 'all' WHERE role_key = 'management'"))
        db.execute(text("UPDATE customer_task_email_reminders SET recipient_user_id = NULL"))
    ensure_activity_responsibility_schema(engine)
    with engine.connect() as db:
        assert db.scalar(text("SELECT recipient_user_id FROM customer_task_email_reminders")) is None
        assert db.scalar(text("SELECT record_scope FROM hub_role_permissions WHERE role_key = 'management'")) == 'all'
    engine.dispose()


def test_assignee_discovery_filters_parent_access_and_saved_scope_is_kept(env):
    result = env.service("manager").query("activities.assignees", {})
    assert {item["name"] for item in result["items"]} == {"Manager", "Steffi", "Colleague"}
    result = env.service("admin").query("activities.assignees", {"customer_id": str(env.excluded.id)})
    assert [item["name"] for item in result["items"]] == ["Admin"]
    permission = env.access.permission(role_key="employee", module_key="activities")
    permission.record_scope = "team"
    env.access.ensure_defaults()
    assert permission.record_scope == "team"
    permission.record_scope = "none"
    env.db.flush()
    assert {item["name"] for item in ActivityResponsibility(env.db, env.users["admin"]).choices()} == {"Admin", "Manager"}


def test_reading_customer_meetings_does_not_complete_other_users_meetings(env):
    from app.services.customer_activities import CustomerActivityService
    row = create(env, "meeting", owner="other", start_date="2020-10-15")
    env.db.commit()
    CustomerActivityService(db=env.db).list_meetings(customer_id=env.customer.id)
    assert row.status == "planned" and not env.db.dirty
