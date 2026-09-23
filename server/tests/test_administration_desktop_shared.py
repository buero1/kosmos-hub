from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func, update

from app.api.routes import accounts, desktop_notifications, web
from app.models.hub_user import HubUser
from app.models.hub_access_control import HubAccessRole, HubTeam, HubRecordAccessGrant
from app.models.customer_activity import CustomerTaskActivity
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.email_ai_prompt_preset import EmailAiPromptPreset
from app.models.email_compose_image import EmailComposeImage
from app.services.hub_operations import HubOperationService, HubOperationError, agent_operations
from app.services.hub_administration import HubAdministrationService, runtime_settings, setting_parameters
from app.services.customer_desktop_reminders import CustomerDesktopReminderService
from app.services.hub_operation_administration import ACCESS_FIELDS
from test_hub_finance_operations import env, req


def native(env, actor="admin"):
    return HubAdministrationService(db=env.db, cipher=env.cipher, actor=actor)


@pytest.mark.parametrize("section,values", [
    ("email_composer", {"font_size": "16"}), ("styling", {"accent_color": "#112233"}),
    ("fleet_refresh", {"max_parallel_site_checks": "3", "auto_refresh_enabled": "false"}),
])
def test_settings_preserve_unmentioned_fields_and_share_native_validation(env, section, values):
    before = env.service.query("settings.read", {"section": section})["values"]
    env.service.execute(f"settings.{section}.update", values)
    after = env.service.query("settings.read", {"section": section})["values"]
    for key in set(before) - values.keys():
        assert after[key] == before[key]
    assert all(str(after[key]).lower() == value.lower() for key, value in values.items())
    native(env).configure(section, **before)
    assert env.service.query("settings.read", {"section": section})["values"] == before
    assert set(after) == set(setting_parameters(section))


@pytest.mark.parametrize("section,values", [("email_composer", {"line_height": "nan"}), ("email_composer", {"font_size": "15"}),
    ("styling", {"accent_color": "red"}), ("fleet_refresh", {"max_parallel_site_checks": "9"}),
    ("fleet_refresh", {"auto_refresh_enabled": "yes"}), ("styling", {"password": "secret"})])
def test_bad_settings_fail_without_changes(env, section, values):
    before = env.service.query("settings.read", {"section": section})
    with pytest.raises(ValueError):
        env.service.execute(f"settings.{section}.update", values)
    assert env.service.query("settings.read", {"section": section}) == before
    assert not env.db.dirty and not env.db.new


def test_settings_ui_uses_same_service(env, monkeypatch):
    monkeypatch.setattr(accounts, "_require_admin_user", lambda request: env.user)
    monkeypatch.setattr(accounts, "require_csrf", lambda *args: None)
    monkeypatch.setattr(accounts, "write_audit_log", lambda *args, **kwargs: None)
    response = accounts.configure_mail_composer_settings(req(env, {}), env.db, "georgia", 16, 1.2, "")
    assert response.status_code == 303
    assert env.service.query("settings.read", {"section": "email_composer"})["values"] == {"font_family_key": "georgia", "font_size": 16, "line_height": 1.2}
    env.service.execute("settings.email_composer.update", {"font_size": "18"})
    assert runtime_settings(env.db, "email_composer").font_size == 18


def test_personal_settings_are_self_only_and_credentials_never_appear(env):
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    limited.execute("settings.personal.update", {"reminder_email": "personal@example.test"})
    assert env.sales.reminder_email == "personal@example.test" and not env.user.reminder_email
    payload = limited.query("settings.read", {})
    assert set(payload) == {"user_id", "username", "reminder_email"}
    with pytest.raises(HubOperationError):
        limited.execute("settings.personal.update", {"reminder_email": "bad@example.test", "user_id": str(env.user.id)})
    for section in ("styling", "fleet_refresh", "signature", "mailbox_alert", "email_prompts"):
        with pytest.raises(HubOperationError):
            limited.query("settings.read", {"section": section})
    for section in ("users", "roles", "teams", "assignments", "grants"):
        with pytest.raises(HubOperationError):
            limited.query("access.read", {"section": section})
    data = json.dumps(env.service.query("access.read", {"section": "users"}))
    assert "password" not in data.split('"notice"')[0] and "session_version" not in data


def test_signature_is_sanitized_and_paged(env):
    env.service.execute("settings.signature.update", {"signature_html": "<p>Signature</p><script>bad()</script>"})
    result = env.service.query("settings.read", {"section": "signature"})
    assert "<script" not in result["signature_html"] and "Signature" in result["signature_html"]


def test_reads_do_not_bootstrap_prompts_or_write_settings(env):
    assert env.db.scalar(select(func.count()).select_from(EmailAiPromptPreset)) == 0
    for section in ("personal", "styling", "email_composer", "fleet_refresh", "signature", "mailbox_alert", "email_prompts"):
        env.service.query("settings.read", {"section": section})
    for section in ("users", "roles", "teams", "assignments", "grants"):
        env.service.query("access.read", {"section": section})
    assert not env.db.new and not env.db.dirty
    assert env.db.scalar(select(func.count()).select_from(EmailAiPromptPreset)) == 0


def test_presets_atomic_validation_and_add(env):
    assert env.service.query("settings.read", {"section": "email_prompts"})["items"] == []
    env.service.execute("settings.email_prompts.update", {})
    items = env.service.query("settings.read", {"section": "email_prompts"})["items"]
    env.service.execute("settings.email_prompts.update", {"presets": json.dumps(items), "new_label": "Test action", "new_instruction": "Test instruction"})
    env.db.commit()
    before = env.service.query("settings.read", {"section": "email_prompts"})
    items = before["items"]
    items[0] = {**items[0], "label": "Changed action"}
    with pytest.raises(ValueError):
        env.service.execute("settings.email_prompts.update", {"presets": json.dumps(items), "new_label": "x", "new_instruction": "x"})
    assert env.service.query("settings.read", {"section": "email_prompts"})["items"][0]["label"] != "Changed action"


def test_admin_roles_teams_users_and_grants_are_shared(env, monkeypatch):
    monkeypatch.setattr(accounts, "_require_admin_user", lambda request: env.user)
    monkeypatch.setattr(accounts, "require_csrf", lambda *args: None)
    monkeypatch.setattr(accounts, "write_audit_log", lambda *args, **kwargs: None)
    env.service.execute("access.roles.save", {"role_key": "test-role", "name": "Test role", "permissions": json.dumps({"customers": {"view": True, "scope": "assigned"}})})
    env.service.execute("access.roles.save", {"role_key": "test-role", "name": "Test role", "permissions": json.dumps({"customers": {"edit": True}})})
    matrix = next(row for row in env.service.query("access.read", {"section": "roles"})["items"] if row["key"] == "test-role")["permissions"]
    assert matrix["customers"]["view"] and matrix["customers"]["edit"] and matrix["customers"]["scope"] == "assigned"
    response = accounts.create_access_team(req(env, {}), env.db, name="Test team", description="Example", csrf_token="")
    assert response.status_code == 303
    team = env.db.scalar(select(HubTeam).where(HubTeam.name == "Test team"))
    env.service.execute("access.users.update", {"user_id": str(env.sales.id), "role": "test-role", "team_id": str(team.id)})
    assert env.sales.role == "test-role" and env.sales.team_id == team.id and env.sales.session_version > 0
    with pytest.raises(ValueError):
        env.service.execute("access.teams.delete", {"team_id": str(team.id)})
    env.service.execute("access.records.assign", {"module_key": "customers", "record_id": str(env.customer.id), "owner_user_id": str(env.sales.id)})
    grant = env.service.execute("access.grants.create", {"module_key": "customers", "record_id": str(env.hidden.id), "team_id": str(team.id), "can_edit": "false"})
    assert env.access.can_access_record(user=env.sales, module_key="customers", record_id=env.hidden.id)
    assert not env.access.can_access_record(user=env.sales, module_key="customers", record_id=env.hidden.id, action="edit")
    env.service.execute("access.grants.delete", {"grant_id": str(grant.record_id)})
    assert not env.access.can_access_record(user=env.sales, module_key="customers", record_id=env.hidden.id)


def test_rejects_privilege_escalation_and_protects_last_admin(env):
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        limited.execute("access.users.update", {"user_id": str(env.sales.id), "role": "admin"})
    for key, data in [("access.users.delete", {"user_id": str(env.user.id)}),
                      ("access.users.update", {"user_id": str(env.user.id), "role": "viewer"}),
                      ("access.roles.save", {"role_key": "admin", "name": "Admin", "permissions": "{}"})]:
        with pytest.raises(ValueError):
            env.service.execute(key, data)
    assert env.user.role == "admin" and env.db.get(HubUser, env.user.id) is env.user
    with pytest.raises(HubOperationError):
        env.service.execute("access.users.update", {"user_id": str(env.sales.id), "password": "not-allowed"})
    assert "access.users.create" not in {operation.key for operation in agent_operations()}
    with pytest.raises(HubOperationError):
        env.service.execute("access.roles.save", {"name": "Bad role", "permissions": '{"customers":{"view":"false"}}'})


def task(env, *, user=None, customer=None):
    now = datetime.now(UTC).replace(tzinfo=None)
    row = CustomerTaskActivity(name="Popup test", customer_id=(customer or env.customer).id, created_by_username=(user or env.user).username,
        created_by_user_id=(user or env.user).id, assignee_user_id=(user or env.user).id,
        due_at=now - timedelta(minutes=1), status="planned", reminder_channel="popup", reminder_minutes_before=0)
    env.db.add(row)
    env.db.commit()
    return row


def test_reminders_query_is_readonly_and_ui_delivers_same_projection(env):
    row = task(env)
    preview = env.service.query("reminders.list", {})["items"]
    assert len(preview) == 1 and preview[0]["id"] is None and preview[0]["activity_id"] == row.id
    assert not env.db.new and not env.db.dirty
    assert env.db.scalar(select(func.count()).select_from(CustomerActivityReminderNotification)) == 0
    delivered = desktop_notifications.list_desktop_reminders(req(env, {}), env.db)["reminders"]
    assert {**preview[0], "id": delivered[0]["id"]} == delivered[0]
    assert env.service.query("reminders.list", {})["items"] == delivered


@pytest.mark.parametrize("action,extra", [("snooze", {"minutes": "5"}), ("snooze_before_start", {"minutes_before": "0"}), ("complete", {})])
def test_virtual_reminders_confirmed_actions_and_rollback(env, action, extra):
    row = task(env)
    reference = env.service.query("reminders.list", {})["items"][0]["reference"]
    result = env.service.execute("reminders." + action, {"reminder_refs": json.dumps([reference]), **extra})
    assert result.outputs["changed"] == "1"
    if action == "complete":
        assert row.status == "completed"
    assert env.service.query("reminders.list", {})["items"] == []


def test_reminder_bulk_failure_is_atomic_and_foreign_refs_are_rejected(env):
    row = task(env)
    reference = env.service.query("reminders.list", {})["items"][0]["reference"]
    for refs, extra in [([reference, "task:999:primary"], {"minutes": "5"}), ([reference], {"minutes": "7"})]:
        with pytest.raises(ValueError):
            env.service.execute("reminders.snooze", {"reminder_refs": json.dumps(refs), **extra})
    assert row.status == "planned"
    assert env.db.scalar(select(func.count()).select_from(CustomerActivityReminderNotification)) == 0
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        limited.execute("reminders.complete", {"reminder_refs": json.dumps([reference])})


def test_revoked_parent_access_hides_reminders_and_blocks_actions(env):
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    env.db.commit()
    row = task(env, user=env.sales)
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    assert limited.query("reminders.list", {})["total"] == 1
    notification = CustomerDesktopReminderService(db=env.db).list_due_reminders(user=env.sales)[0]
    env.db.commit()
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=None, team_id=None)
    env.db.commit()
    assert limited.query("reminders.list", {})["total"] == 0
    with pytest.raises(ValueError):
        limited.execute("reminders.complete", {"notification_ids": json.dumps([notification.id])})
    assert row.status == "planned"


@pytest.mark.parametrize("action", list(ACCESS_FIELDS))
def test_every_admin_mutation_rechecks_current_role(env, action):
    values = {field.name: "1" for field in ACCESS_FIELDS[action] if field.required}
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError, match="Berechtigung"):
        limited.execute("access." + action, values)


def test_demoted_actor_cannot_use_cached_admin_role(env):
    env.db.commit()
    env.db.execute(update(HubUser).where(HubUser.id == env.user.id).values(role="viewer").execution_options(synchronize_session=False))
    assert env.user.role == "admin"
    with pytest.raises(HubOperationError, match="Berechtigung"):
        env.service.execute("access.teams.create", {"name": "Forbidden"})
    assert env.db.scalar(select(HubTeam).where(HubTeam.name == "Forbidden")) is None


def test_missing_setting_is_not_implicit_clear(env):
    for key in ("settings.personal.update", "settings.signature.update"):
        with pytest.raises(HubOperationError):
            env.service.execute(key, {})
    env.service.execute("settings.signature.update", {"signature_html": ""})
    assert env.service.query("settings.read", {"section": "signature"})["signature_html"] == ""


def test_existing_reminder_reference_survives_popup_configuration_change(env):
    row = task(env)
    notification = CustomerDesktopReminderService(db=env.db).list_due_reminders(user=env.user)[0]
    row.reminder_channel = None
    env.db.commit()
    assert env.service.query("reminders.list", {})["items"][0]["reference"] == notification.reference
    result = env.service.execute("reminders.snooze", {"reminder_refs": json.dumps([notification.reference]), "minutes": "5"})
    assert result.outputs["changed"] == "1"


@pytest.mark.parametrize("commit", [False, True])
def test_user_image_cleanup_waits_for_successful_commit(env, monkeypatch, commit):
    removed = []
    monkeypatch.setattr("app.services.hub_administration.EmailComposeImageService",
                        lambda **kwargs: SimpleNamespace(storage=SimpleNamespace(remove=removed.append)))
    image = EmailComposeImage(token="test-token", storage_key="test-key", filename="test.png", content_type="image/png",
                              byte_size=1, created_by_user_id=env.sales.id)
    user_id = env.sales.id
    env.db.add(image)
    env.db.commit()
    env.service.execute("access.users.delete", {"user_id": str(user_id)})
    assert removed == []
    if commit:
        env.db.commit()
        assert removed == ["test-key"]
        assert env.db.get(HubUser, user_id) is None
    else:
        env.db.rollback()
        env.db.commit()
        assert removed == []
        assert env.db.get(HubUser, user_id) is not None


def test_access_users_read_paginates_without_secrets(env):
    env.db.add_all(HubUser(username=f"member-{index}", password_hash="NEVER-EXPOSE", role="viewer") for index in range(30))
    env.db.commit()
    first = env.service.query("access.read", {"section": "users"})
    second = env.service.query("access.read", {"section": "users", "offset": first["next_offset"]})
    assert len(first["items"]) == 25 and second["next_offset"] == ""
    assert len({item["id"] for item in first["items"] + second["items"]}) == first["total"]
    assert "NEVER-EXPOSE" not in json.dumps([first, second])


@pytest.mark.parametrize("section, values", [("styling", {}), ("email_composer", {}), ("fleet_refresh", {}),
    ("signature", {"signature_html": ""}), ("email_prompts", {}), ("mailbox_alert", {"mailbox_alert_email": "x@example.test"})])
def test_global_settings_mutations_are_admin_only(env, section, values):
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError, match="Berechtigung"):
        limited.execute(f"settings.{section}.update", values)
