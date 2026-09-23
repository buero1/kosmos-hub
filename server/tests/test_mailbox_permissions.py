import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.db.base import Base
from app.core.security import SecretCipher
from app.core.mailbox_actor import mailbox_actor
from app.core.templates import create_templates
from app.models.hub_user import HubUser
from app.models.hub_access_control import HubTeam, HubRolePermission
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_mailbox_permission import HubMailboxMembership
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.services.hub_accounts import HubAccountService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_administration import HubAdministrationService
from app.services.hub_mailbox_permissions import MAILBOX_ACTIONS, MAILBOX_ACTION_LABELS, MailboxPermissions, bind_message, mailbox_access_snapshot, save_mailbox_permissions
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_access import HubMailboxAccess
from app.services.hub_mailbox_transport import HubMailboxTransportService
from app.services.hub_operations import HubOperationService
from app.services.customer_communications import CustomerCommunicationService
from app.services.scheduled_emails import ScheduledEmailService


@pytest.fixture
def env(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        cipher = SecretCipher("mailbox-permission-tests")
        access = HubAccessControlService(db=db)
        access.ensure_defaults()
        access.save_role(role_key="mail-staff", name="Mail staff", description="", permissions={
            "emails": {action: True for action in (*MAILBOX_ACTIONS, "manage", "export")},
            "customers": {"view": True, "scope": "all"}, "leads": {"view": True, "scope": "all"}})
        team = HubTeam(name="Sales")
        admin = HubUser(username="admin", role="admin", password_hash="x")
        db.add_all([team, admin])
        db.flush()
        staff = HubUser(username="staff", role="mail-staff", team_id=team.id, password_hash="x")
        boxes = [HubMailboxAccount(email_address=f"{name}@example.test", display_name=name, username=name,
            encrypted_password=cipher.encrypt("secret"), enabled=True, verified_at=datetime.now(UTC)) for name in ("info", "billing")]
        customer = Customer(name="Customer")
        db.add_all([staff, customer, *boxes])
        db.flush()
        yield SimpleNamespace(db=db, cipher=cipher, access=access, staff=staff, admin=admin, team=team, boxes=boxes, customer=customer, tmp_path=tmp_path)
    engine.dispose()


def grant(env, *, subject="team", box=0, **overrides):
    values = {action: True if subject == "team" else None for action in MAILBOX_ACTIONS}
    values.update(overrides)
    save_mailbox_permissions(env.db, actor="admin", subject_type=subject,
        subject_id=env.team.id if subject == "team" else env.staff.id,
        entries={str(env.boxes[box].id): values})


def message(env, *, box=0, linked=False, source=True):
    payload = {"subject": f"Private {box}", "from": "person@external.test", "to": env.boxes[box].email_address,
               "content": "<p>Confidential</p>"}
    if source:
        payload["mittwald_mailbox"] = env.boxes[box].email_address
    fields = {"source": "mittwald-imap", "direction": "inbound", "is_unread": True, "encrypted_payload_json": env.cipher.encrypt(json.dumps(payload))}
    row = CustomerZohoEmail(customer_id=env.customer.id, zoho_message_id=f"msg-{box}", **fields) if linked else HubMailboxEmail(fingerprint=f"message-{box}", received_at=datetime.now(UTC), **fields)
    env.db.add(row)
    env.db.flush()
    bind_message(env.db, env.cipher, row)
    return row


def service(env, actor="staff"):
    return HubMailboxService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test", actor=actor)


@pytest.mark.parametrize("action", MAILBOX_ACTIONS)
def test_team_inheritance_user_denial_and_role_ceiling(env, action):
    policy = lambda: MailboxPermissions(db=env.db, actor="staff")
    assert not policy().can(env.boxes[0].id, action)
    grant(env)
    assert policy().can(env.boxes[0].id, action)
    assert not policy().can(env.boxes[1].id, action)
    grant(env, subject="user", **{action: False})
    assert not policy().can(env.boxes[0].id, action)
    grant(env, subject="user", **{action: True})
    assert policy().can(env.boxes[0].id, action)
    permission = env.access.permission(role_key="mail-staff", module_key="emails")
    setattr(permission, f"can_{action}", False)
    env.db.flush()
    assert not policy().can(env.boxes[0].id, action)


def test_no_read_permission_blocks_all_actions_even_with_explicit_allow(env):
    grant(env)
    grant(env, subject="user", view=False, send=True)
    assert not any(MailboxPermissions(db=env.db, actor="staff").can(env.boxes[0].id, a) for a in MAILBOX_ACTIONS)


def test_team_change_deactivation_and_new_mailbox_deny(env):
    grant(env)
    assert MailboxPermissions(db=env.db, user=env.staff).can(env.boxes[0].id)
    env.team.is_active = False
    assert not MailboxPermissions(db=env.db, user=env.staff).can(env.boxes[0].id)
    env.team.is_active = True
    env.staff.team_id = None
    assert not MailboxPermissions(db=env.db, user=env.staff).can(env.boxes[0].id)
    grant(env, subject="user", view=True)
    assert MailboxPermissions(db=env.db, user=env.staff).can(env.boxes[0].id)
    env.staff.is_active = False
    env.db.flush()
    assert not MailboxPermissions(db=env.db, actor="staff").can(env.boxes[0].id)


@pytest.mark.parametrize("linked", [True, False])
def test_lists_counts_direct_reads_and_attachments_are_scoped(env, linked):
    grant(env)
    allowed, hidden = message(env, box=0, linked=linked), message(env, box=1, linked=linked)
    mailbox = service(env)
    assert len(mailbox.get_folder_view(folder="inbox", unread_only=False).messages) == 1
    assert mailbox.get_folder_counts()["inbox"] == mailbox.get_unread_count() == 1
    key = f"linked-{env.customer.id}-{hidden.id}" if linked else f"unassigned-{hidden.id}"
    assert mailbox.get_selected_message(folder="inbox", unread_only=False, selected_key=key) is None
    gateway = HubOperationService(db=env.db, cipher=env.cipher, actor="staff")
    assert gateway.query("emails.list", {"folder": "inbox"})["total"] == 1
    for query, values in (("emails.read", {"email_key": key}), ("emails.attachments.list", {"email_key": key})):
        with pytest.raises(ValueError):
            gateway.query(query, values)
    with pytest.raises(ValueError):
        mailbox.apply_batch_action(keys=[key], action="move_trash")
    assert hidden.mailbox_state == "active"
    if linked:
        view = CustomerCommunicationService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test").get_view(customer_id=env.customer.id, actor="staff")
        assert [item.id for item in view.emails] == [allowed.id]


def test_crm_visibility_cannot_open_a_denied_mailbox(env):
    row = message(env, linked=True)
    assert env.access.can_access_record(user=env.staff, module_key="customers", record_id=env.customer.id)
    assert not HubMailboxAccess(db=env.db, cipher=env.cipher, actor="staff").visible(row)
    grant(env)
    env.access.permission(role_key="mail-staff", module_key="customers").can_view = False
    env.db.flush()
    assert not HubMailboxAccess(db=env.db, cipher=env.cipher, actor="staff").visible(row)


def test_ambiguous_legacy_message_denied_and_backfill_idempotent(env):
    grant(env)
    payload = {"subject": "Legacy", "to": [box.email_address for box in env.boxes], "from": "external@example.test"}
    row = HubMailboxEmail(fingerprint="ambiguous", received_at=datetime.now(UTC), encrypted_payload_json=env.cipher.encrypt(json.dumps(payload)))
    env.db.add(row)
    env.db.flush()
    assert not bind_message(env.db, env.cipher, row)
    assert not HubMailboxAccess(db=env.db, cipher=env.cipher, actor="staff").visible(row)
    assert HubMailboxAccess(db=env.db, cipher=env.cipher, actor="admin").visible(row)
    payload["mittwald_mailbox"] = env.boxes[1].email_address
    row.encrypted_payload_json = env.cipher.encrypt(json.dumps(payload))
    assert bind_message(env.db, env.cipher, row)
    assert bind_message(env.db, env.cipher, row)
    assert env.db.scalar(select(func.count()).select_from(HubMailboxMembership)) == 1
    assert not HubMailboxAccess(db=env.db, cipher=env.cipher, actor="staff").visible(row)


def test_readonly_mailbox_does_not_allow_mutation_or_smtp(env, monkeypatch):
    grant(env, create=False, edit=False, send=False, delete=False)
    row = message(env)
    mailbox = service(env)
    for action in ("move_trash", "mark_unread", "move_spam"):
        with pytest.raises(ValueError):
            mailbox.apply_batch_action(keys=[f"unassigned-{row.id}"], action=action)
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *args, **kwargs: pytest.fail("SMTP must not be called"))
    with pytest.raises(ValueError, match="Berechtigung"):
        HubMailboxTransportService(db=env.db, cipher=env.cipher, actor="staff").send(sender_email=env.boxes[0].email_address,
            recipient_name="Test", recipient_email="other@example.test", subject="Test", html_content="Test", cc_recipients=(), reply_to_message_id=None, attachments=())


def test_request_identity_cannot_be_overridden_by_service_actor(env):
    token = mailbox_actor.set("staff")
    try:
        assert service(env, actor="admin").scope.user.username == "staff"
        assert not MailboxPermissions(db=env.db, actor="admin").can(env.boxes[0].id)
    finally:
        mailbox_actor.reset(token)
    assert mailbox_actor.get() is None


def test_personal_address_is_not_a_grant_and_default_requires_send(env):
    accounts = HubAccountService(db=env.db, app_secret_key="test")
    accounts.update_user(user_id=env.staff.id, username="staff", role="mail-staff", team_id=env.team.id, email_address=env.boxes[0].email_address)
    assert env.staff.email_address == env.boxes[0].email_address
    assert not MailboxPermissions(db=env.db, actor="staff").can(env.boxes[0].id)
    grant(env)
    accounts.update_user(user_id=env.staff.id, username="staff", role="mail-staff", team_id=env.team.id, default_sender_account_id=str(env.boxes[0].id))
    assert MailboxPermissions(db=env.db, actor="staff").default_sender() == env.boxes[0].email_address
    with pytest.raises(ValueError, match="Sendeberechtigung"):
        accounts.update_user(user_id=env.staff.id, username="staff", role="mail-staff", team_id=env.team.id, default_sender_account_id=str(env.boxes[1].id))


def test_shared_grant_operation_validation_and_ui_retains_selection(env):
    grant(env)
    grant(env, subject="user", send=False)
    admin = HubOperationService(db=env.db, cipher=env.cipher, actor="admin")
    snapshot = admin.query("access.mailboxes.read", {})
    assert snapshot["effective"][env.staff.id][env.boxes[0].id]["view"]
    assert not snapshot["effective"][env.staff.id][env.boxes[0].id]["send"]
    assert "password" not in json.dumps(snapshot)
    entries = {str(env.boxes[1].id): {action: True for action in MAILBOX_ACTIONS}}
    with pytest.raises(ValueError):
        HubOperationService(db=env.db, cipher=env.cipher, actor="staff").execute("access.mailboxes.update", {
            "subject_type": "team", "subject_id": str(env.team.id), "permissions": json.dumps(entries)})
    admin.execute("access.mailboxes.update", {"subject_type": "team", "subject_id": str(env.team.id), "permissions": json.dumps(entries)})
    assert MailboxPermissions(db=env.db, actor="staff").can(env.boxes[1].id, "send")
    html = create_templates(directory="app/templates").get_template("partials/mailbox_access_form.html").render(
        mailbox_access=mailbox_access_snapshot(env.db), mailbox_subject_type="user", mailbox_subject_id=env.staff.id,
        mailbox_action_labels=MAILBOX_ACTION_LABELS, account_user=env.staff, csrf_token="test")
    select_html = html.split(f'name="mailbox__{env.boxes[0].id}__send"', 1)[1].split("</select>", 1)[0]
    assert '<option value="false" selected>' in select_html


def test_draft_sender_change_checks_both_mailboxes_and_replaces_membership(env):
    grant(env)
    gateway = HubOperationService(db=env.db, cipher=env.cipher, actor="staff")
    result = gateway.execute("emails.drafts.create", {"recipient_email": "recipient@example.test", "subject": "Test", "content": "Test"})
    draft = env.db.get(HubMailboxEmail, result.record_id)
    assert MailboxPermissions(db=env.db, actor="staff").message_allowed(draft)
    with pytest.raises(ValueError):
        gateway.execute("emails.drafts.update", {"draft_id": str(draft.id), "sender_email": env.boxes[1].email_address})
    grant(env, box=1)
    gateway.execute("emails.drafts.update", {"draft_id": str(draft.id), "sender_email": env.boxes[1].email_address})
    ids = list(env.db.scalars(select(HubMailboxMembership.mailbox_account_id).where(HubMailboxMembership.mailbox_email_id == draft.id)))
    assert ids == [env.boxes[1].id]


def test_scheduled_delivery_rechecks_revoked_permissions(env, monkeypatch):
    grant(env)
    scheduled = ScheduledEmailService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test")
    row = scheduled.schedule(actor="staff", scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        sender_email=env.boxes[0].email_address, recipient_email="someone@example.test", recipient_name="Someone", subject="Test", content="Test", cc_emails="")
    grant(env, subject="user", send=False)
    row.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    env.db.commit()
    monkeypatch.setattr(HubMailboxTransportService, "send", lambda *args, **kwargs: pytest.fail("Revoked send permission must stop delivery"))
    outcome = scheduled._deliver(scheduled_id=row.id)
    assert outcome.sent == 0
    env.db.refresh(row)
    assert row.status != "sent"


def test_agent_cannot_add_hidden_email_or_reuse_revoked_context(env):
    from app.services.hub_agent import HubAgentService
    grant(env)
    row = message(env)
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    chat = agent.add_context(actor="staff", resource_type="email", resource_key=f"unassigned-{row.id}")
    from app.models.hub_agent import HubAgentConversation
    conversation = env.db.get(HubAgentConversation, chat.conversation_id)
    grant(env, subject="user", view=False)
    with pytest.raises(ValueError):
        agent.add_context(actor="staff", resource_type="email", resource_key=f"unassigned-{row.id}")
    assert not agent._conversation_email_contexts(conversation)
    assert not agent._conversation_prompt_contexts(conversation, exclude_email_key="")


def test_draft_delivery_is_checked_before_cleanup_or_smtp(env):
    grant(env)
    result = HubOperationService(db=env.db, cipher=env.cipher, actor="staff").execute("emails.drafts.create", {
        "recipient_email": "recipient@example.test", "subject": "Keep", "content": "Keep"})
    grant(env, subject="user", send=False)
    # Reading/downloading an attachment is not permission to send the draft.
    assert service(env).draft_attachments(draft_id=result.record_id) == ()
    with pytest.raises(ValueError):
        service(env).prepare_draft_delivery_attachments(draft_id=result.record_id)
    with pytest.raises(ValueError):
        service(env).discard_draft(draft_id=result.record_id)
    assert env.db.get(HubMailboxEmail, result.record_id).mailbox_state == "draft"


def test_native_customer_reads_and_forwarding_enforce_request_mailbox(env):
    row = message(env, box=1, linked=True)
    native = CustomerCommunicationService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test")
    token = mailbox_actor.set("staff")
    try:
        for callback, values in (
            (native.get_email_reply, {}), (native.get_email_forward, {}),
            (native.download_email_attachment, {"attachment_id": "unknown"}),
            (native.get_email_preview_image, {"source_url_hash": "a" * 64}),
            (native.mark_email_read, {}),
        ):
            with pytest.raises(ValueError, match="verfuegbar"):
                callback(customer_id=env.customer.id, email_id=row.id, **values)
    finally:
        mailbox_actor.reset(token)
    assert row.is_unread


def test_navigation_badge_counts_only_granted_mailboxes(env, monkeypatch):
    from contextlib import nullcontext
    from app.core import templates
    grant(env)
    message(env)
    message(env, box=1)
    monkeypatch.setattr(templates, "SessionLocal", lambda: nullcontext(env.db))
    monkeypatch.setattr("app.core.security.get_secret_cipher", lambda: env.cipher)
    assert templates._unread_email_count(env.staff) == 1
    grant(env, subject="user", view=False)
    assert templates._unread_email_count(env.staff) == 0


def test_native_scheduled_read_cancel_and_send_now_check_current_user(env):
    grant(env)
    native = ScheduledEmailService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test")
    row = native.schedule(actor="admin", scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        sender_email=env.boxes[1].email_address, recipient_email="someone@example.test", recipient_name="Someone", subject="Test", content="Test", cc_emails="")
    token = mailbox_actor.set("staff")
    try:
        for callback in (native.get_compose_context, native.cancel, native.send_now):
            with pytest.raises(ValueError, match="verfuegbar"):
                callback(scheduled_email_id=row.id)
    finally:
        mailbox_actor.reset(token)
    assert row.status == "scheduled"


def test_mysql_schema_retains_cascades_without_incompatible_checks():
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable
    from app.models.hub_mailbox_permission import HubMailboxPermission
    for model in (HubMailboxPermission, HubMailboxMembership):
        sql = str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))
        assert "CHECK (" not in sql and "ON DELETE CASCADE" in sql


def test_revoked_default_sender_does_not_prevent_saving_user(env):
    grant(env)
    accounts = HubAccountService(db=env.db, app_secret_key="test")
    values = dict(user_id=env.staff.id, username="staff", role="mail-staff", team_id=env.team.id,
                  default_sender_account_id=str(env.boxes[0].id))
    accounts.update_user(**values)
    grant(env, subject="user", send=False)
    accounts.update_user(**values, email_address="staff@example.test")
    assert env.staff.default_sender_account_id is None
    assert env.staff.email_address == "staff@example.test"


@pytest.mark.parametrize("kind", ["permission", "membership"])
def test_mailbox_single_target_is_validated_in_orm(env, kind):
    from app.models.hub_mailbox_permission import HubMailboxPermission
    row = HubMailboxPermission(mailbox_account_id=env.boxes[0].id, user_id=env.staff.id, team_id=env.team.id) if kind == "permission" else HubMailboxMembership(mailbox_account_id=env.boxes[0].id)
    env.db.add(row)
    with pytest.raises(ValueError, match="Exactly one"):
        env.db.flush()
    env.db.rollback()
