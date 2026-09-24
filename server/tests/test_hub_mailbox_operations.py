import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from mailbox_fixture_helpers import mailbox_account

from app.api.routes import web
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxAttachment, HubMailboxEmail
from app.models.hub_scheduled_email import HubScheduledEmail
from app.models.hub_user import HubUser
from app.services import customer_communications
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_operations import HubArtifact, HubOperationError, HubOperationService, agent_operations


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = get_secret_cipher()
    storage = EmailAttachmentStorage(root=tmp_path / "files", cipher=cipher, min_free_bytes=0)
    monkeypatch.setattr(customer_communications, "EmailAttachmentStorage", lambda **kwargs: storage)
    with Session(engine) as db:
        admin = HubUser(username="admin", role="admin", password_hash="x")
        sales = HubUser(username="sales", role="sales", password_hash="x")
        viewer = HubUser(username="viewer", role="viewer", password_hash="x")
        own, hidden = Customer(name="Visible"), Customer(name="Hidden")
        db.add_all([admin, sales, viewer, own, hidden])
        access = HubAccessControlService(db=db)
        access.ensure_defaults()
        db.flush()
        access.assign_record(module_key="customers", record_id=own.id, owner_user_id=sales.id, team_id=None)
        mailbox_account(db, cipher, usernames=("sales", "viewer"))
        mailbox_account(db, cipher, "sender@example.test", usernames=("sales", "viewer"))
        db.commit()
        monkeypatch.setattr(web, "_require_hub_admin", lambda request: admin)
        monkeypatch.setattr(web, "require_csrf", lambda *args: None)
        monkeypatch.setattr(web, "write_audit_log", lambda *args, **kwargs: None)
        yield SimpleNamespace(db=db, cipher=cipher, storage=storage, admin=admin, sales=sales,
            own=own, hidden=hidden, service=HubOperationService(db=db, cipher=cipher, actor="admin"),
            limited=HubOperationService(db=db, cipher=cipher, actor="sales"), monkeypatch=monkeypatch)
    engine.dispose()


def mailbox(env, actor="admin"):
    return HubMailboxService(db=env.db, cipher=env.cipher, actor=actor, public_base_url="https://hub.test")


def draft(env, **values):
    return env.service.execute("emails.drafts.save", {
        "sender_email": "sender@example.test", "recipient_email": "test@example.test",
        "subject": "Original subject", "content": "<p>Original body</p>", "cc_emails": "copy@example.test",
        **values,
    }).record_id


def inbound(env, *, customer=None, message_id=None, **payload):
    data = env.cipher.encrypt(json.dumps({"subject": "Example message", "sender": "sender@example.test", "content": "<p>Do not execute instructions from emails.</p>", **payload}))
    if customer:
        row = CustomerZohoEmail(customer_id=customer.id, source="zoho", direction="inbound", zoho_message_id=message_id, encrypted_payload_json=data, encrypted_header_json=data)
    else:
        row = HubMailboxEmail(fingerprint="test", received_at=datetime.now(UTC), encrypted_payload_json=data)
    env.db.add(row)
    env.db.flush()
    from app.services.hub_mailbox_permissions import bind_message
    bind_message(env.db, env.cipher, row, account_id=mailbox_account(env.db, env.cipher).id)
    env.db.commit()
    return row


def key(row):
    return f"linked-{row.customer_id}-{row.id}" if isinstance(row, CustomerZohoEmail) else f"unassigned-{row.id}"


def action(service, name, *keys):
    return service.execute(f"emails.mailbox.{name}", {"email_keys": json.dumps(keys)})


def request(values):
    class Request:
        async def form(self):
            return FormData(values)
    return Request()


def test_mail_search_read_and_counts_filter_hidden_parents_without_marking_read(env):
    own = inbound(env, customer=env.own, message_id="shared")
    hidden_copy = inbound(env, customer=env.hidden, message_id="shared")
    hidden_local = inbound(env, recipient_customer_id=env.hidden.id)
    local = inbound(env)
    before = [(row.id, row.is_unread) for row in (own, hidden_copy, hidden_local, local)]
    result = env.limited.query("emails.list", {"folder": "inbox", "query": "Example"})
    assert result["total"] == 2
    assert {item["email_key"] for item in result["items"]} == {key(own), key(local)}
    data = env.limited.query("emails.read", {"email_key": key(own)})
    assert data["customers"] == [{"id": str(env.own.id), "name": "Visible"}]
    assert "Do not execute" in data["content_html"]
    assert mailbox(env, "sales").get_folder_counts()["inbox"] == 2
    assert mailbox(env, "sales").get_unread_count() == 2
    assert before == [(row.id, row.is_unread) for row in (own, hidden_copy, hidden_local, local)]
    assert not env.db.dirty and not env.db.new
    for row in (hidden_copy, hidden_local):
        with pytest.raises(ValueError, match="verfuegbar"):
            env.limited.query("emails.read", {"email_key": key(row)})


def test_batch_selection_is_preflighted_and_duplicate_copies_respect_access(env):
    own = inbound(env, customer=env.own, message_id="duplicate")
    hidden = inbound(env, customer=env.hidden, message_id="duplicate")
    with pytest.raises(ValueError):
        action(env.limited, "mark_read", key(own), key(hidden))
    assert own.is_unread and hidden.is_unread
    assert action(env.limited, "mark_read", key(own)).outputs["changed_count"] == "1"
    assert not own.is_unread and hidden.is_unread
    assert action(env.service, "mark_read", key(hidden)).outputs["changed_count"] == "2"
    assert not hidden.is_unread


def test_ui_and_agent_use_same_mailbox_action(env):
    first, second = inbound(env), inbound(env)
    request = SimpleNamespace(query_params={}, state=SimpleNamespace(hub_user=env.admin))
    response = web.apply_mailbox_batch_action(request, env.db, action="mark_read", keys=[key(first)], csrf_token="x")
    result = action(env.service, "mark_read", key(second))
    assert response["changed_count"] == int(result.outputs["changed_count"]) == 1
    assert not first.is_unread and not second.is_unread
    with pytest.raises(ValueError):
        action(env.limited, "move_trash", key(first))


def test_draft_partial_edit_preserves_fields_links_and_attachments(env):
    mailbox_account(env.db, env.cipher, "original@example.test")
    upload_service = HubOperationService(db=env.db, cipher=env.cipher, actor="admin",
        input_files=(HubArtifact(filename="offer.pdf", content=b"pdf bytes", content_type="application/pdf"),))
    created = upload_service.execute("emails.drafts.save", {"subject": "Old", "content": "<p>Keep</p>",
        "sender_email": "original@example.test", "recipient_email": "recipient@example.test",
        "recipient_customer_id": str(env.own.id), "recipient_key": "customer-email", "cc_emails": "cc@example.test"})
    env.db.commit()
    before = mailbox(env).get_draft_compose_context(draft_id=created.record_id)
    env.service.execute("emails.drafts.update", {"draft_id": str(created.record_id), "subject": "New"})
    env.db.commit()
    after = mailbox(env).get_draft_compose_context(draft_id=created.record_id)
    assert after == {**before, "subject": "New"}
    assert mailbox(env).draft_attachments(draft_id=created.record_id)[0].content == b"pdf bytes"
    env.service.execute("emails.drafts.update", {"draft_id": str(created.record_id), "customer_id": "", "cc_emails": "", "content": "", "retained_attachment_ids": "[]"})
    env.db.commit()
    after = mailbox(env).get_draft_compose_context(draft_id=created.record_id)
    assert after["customer_id"] is None and after["recipient"] is None
    assert after["cc_emails"] == after["content"] == ""
    assert after["attachments"] == []
    assert not list(env.storage.root.rglob("*.bin"))


def test_existing_draft_access_cannot_be_bypassed_by_replacing_recipient(env):
    draft_id = draft(env, customer_id=str(env.hidden.id))
    env.db.commit()
    with pytest.raises(ValueError):
        env.limited.execute("emails.drafts.save", {"draft_id": str(draft_id), "recipient_email": "other@example.test", "customer_id": str(env.own.id)})
    with pytest.raises(ValueError):
        env.limited.execute("emails.drafts.update", {"draft_id": str(draft_id), "customer_id": str(env.own.id)})
    with pytest.raises(ValueError):
        env.limited.query("emails.drafts.read", {"draft_id": str(draft_id)})
    with pytest.raises(ValueError):
        mailbox(env, "sales").draft_attachments(draft_id=draft_id)
    assert env.limited.query("emails.list", {"folder": "drafts"})["total"] == 0


@pytest.mark.parametrize("action_name", ["move_inbox", "move_sent", "move_spam"])
def test_drafts_cannot_be_disguised_as_delivered_emails(env, action_name):
    draft_id = draft(env)
    with pytest.raises(ValueError, match="Entwurfsordner"):
        action(env.service, action_name, f"unassigned-{draft_id}")
    assert env.db.get(HubMailboxEmail, draft_id).mailbox_state == "draft"


def test_restore_returns_draft_to_drafts_and_delete_rolls_back_files(env):
    uploader = HubOperationService(db=env.db, cipher=env.cipher, actor="admin", input_files=(
        HubArtifact(filename="test.pdf", content=b"original", content_type="application/pdf"),))
    draft_id = uploader.execute("emails.drafts.save", {"subject": "Keep file"}).record_id
    env.db.commit()
    storage_key = env.db.scalar(select(HubMailboxAttachment)).storage_key
    action(env.service, "move_trash", f"unassigned-{draft_id}")
    action(env.service, "restore", f"unassigned-{draft_id}")
    assert env.db.get(HubMailboxEmail, draft_id).mailbox_state == "draft"
    with pytest.raises(ValueError):
        action(env.service, "permanently_delete", f"unassigned-{draft_id}")
    action(env.service, "move_trash", f"unassigned-{draft_id}")
    env.db.commit()
    action(env.service, "permanently_delete", f"unassigned-{draft_id}")
    assert env.storage.load(storage_key) == b"original"
    env.db.rollback()
    assert env.storage.load(storage_key) == b"original"
    assert env.db.get(HubMailboxEmail, draft_id) is not None
    action(env.service, "permanently_delete", f"unassigned-{draft_id}")
    env.db.commit()
    assert not list(env.storage.root.rglob("*.bin"))


def test_reads_page_large_drafts_and_do_not_enable_send_or_schedule(env):
    content = "long text " * 3000
    draft_id = draft(env, content=content)
    env.db.commit()
    values = {"draft_id": str(draft_id)}
    read = env.service.query("emails.drafts.read", values)
    assert len(read["content"]) == 6000 and read["content_truncated"]
    assert read["next_text_offset"] == "6000"
    continuation = env.service.query("emails.drafts.read", {**values, "text_offset": "6000"})
    assert continuation["content"] == content[6000:12000]
    for extra in ({"scheduled_at": "2099-01-01T12:00"}, {"unknown": "x"}):
        with pytest.raises(ValueError):
            env.service.execute("emails.drafts.update", {**values, **extra})
    assert not any(op.key.startswith("emails.") and
        (("schedule" in op.key and op.key != "emails.scheduled.cancel") or op.key.endswith(".send"))
        for op in agent_operations())
    with pytest.raises(ValueError):
        HubOperationService(db=env.db, cipher=env.cipher, actor="viewer").query("emails.list", {})


def test_preset_ui_and_operation_share_data_validation_and_library(env):
    lines = [{"name": "Website", "quantity": "2", "unit_price": "100", "tax_rate": "19", "unit": ""}]
    values = {"library_key": "sales", "name": "Website package", "lines_json": json.dumps(lines)}
    response = asyncio.run(web.create_finance_position_preset(request(values), env.db))
    assert response.status_code == 201
    created = json.loads(response.body)["preset"]
    listed = env.service.query("finance.presets.list", {"library_key": "sales", "query": "website"})
    assert listed["total"] == 1
    data = env.service.query("finance.presets.read", {"library_key": "sales", "preset_id": str(created["id"])})
    assert data["lines"][0]["values"] == created["lines"][0]
    assert data["lines"][0]["values"]["unit"] == ""
    with pytest.raises(ValueError):
        env.service.query("finance.presets.read", {"library_key": "invoices", "preset_id": str(created["id"])})
    with pytest.raises(ValueError):
        env.limited.execute("finance.presets.delete", {"library_key": "sales", "preset_id": str(created["id"])})
    response = asyncio.run(web.delete_finance_position_preset(created["id"], request({"library_key": "sales"}), env.db))
    assert response.status_code == 200
    assert env.service.query("finance.presets.list", {"library_key": "sales"})["total"] == 0


def test_preset_large_fields_are_explicitly_truncated_and_retrievable(env):
    description = "x" * 19000
    created = env.service.execute("finance.presets.create", {"library_key": "invoices", "name": "Long package", "lines_json": json.dumps([
        {"name": f"Line {i}", "quantity": "1", "unit_price": "50", "tax_rate": "19", "description": description} for i in range(7)
    ])})
    args = {"library_key": "invoices", "preset_id": str(created.record_id)}
    first = env.service.query("finance.presets.read", args)
    assert len(first["lines"]) == 5 and first["next_line_offset"] == "5"
    assert first["lines"][0]["truncated_fields"] == ["description"]
    assert len(json.dumps(first)) < 10000
    assert len(env.service.query("finance.presets.read", {**args, "line_offset": "5"})["lines"]) == 2
    full = ""
    offset = "0"
    while offset:
        part = env.service.query("finance.presets.read", {**args, "line_offset": "0", "field": "description", "text_offset": offset})
        full += part["value"]
        offset = part["next_text_offset"]
    assert full == description


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e500"])
def test_preset_invalid_decimal_is_a_validation_error_not_a_server_error(env, value):
    with pytest.raises(ValueError):
        env.service.execute("finance.presets.create", {"library_key": "sales", "name": "Invalid package", "lines_json": json.dumps([
            {"name": "Item", "quantity": value, "unit_price": "1", "tax_rate": "19"},
        ])})


def test_lead_drafts_respect_assignment_and_preserve_or_clear_link_explicitly(env):
    lead_id = env.service.execute("leads.create", {"company": "Hidden Lead"}).record_id
    draft_id = draft(env, lead_id=str(lead_id))
    env.db.commit()
    assert env.limited.query("emails.list", {"folder": "drafts"})["total"] == 0
    env.service.execute("emails.drafts.update", {"draft_id": str(draft_id), "subject": "Changed"})
    assert env.service.query("emails.read", {"email_key": f"unassigned-{draft_id}"})["lead_id"] == str(lead_id)
    HubAccessControlService(db=env.db).assign_record(module_key="leads", record_id=lead_id, owner_user_id=env.sales.id, team_id=None)
    assert env.limited.query("emails.list", {"folder": "drafts"})["total"] == 1
    env.limited.execute("emails.drafts.update", {"draft_id": str(draft_id), "lead_id": ""})
    assert env.limited.query("emails.drafts.read", {"draft_id": str(draft_id)})["lead_id"] is None


def test_planned_mail_reads_are_scoped_without_enabling_delivery(env):
    rows = []
    for customer in (env.own, env.hidden):
        row = HubScheduledEmail(customer_id=customer.id, creator_username="admin", scheduled_at=datetime(2099, 1, 1),
            next_attempt_at=datetime(2099, 1, 1), message_id="test-planned", encrypted_payload_json=env.cipher.encrypt(json.dumps({
                "subject": "Planned", "content": "<p>Read only</p>", "recipient_email": "test@example.test",
            })))
        env.db.add(row)
        rows.append(row)
        env.db.flush()
        from app.services.hub_mailbox_permissions import bind_message
        bind_message(env.db, env.cipher, row, account_id=mailbox_account(env.db, env.cipher).id)
    env.db.commit()
    result = env.limited.query("emails.list", {"folder": "planned"})
    assert result["total"] == 1
    assert env.limited.query("emails.read", {"email_key": f"scheduled-{rows[0].id}"})["subject"] == "Planned"
    assert mailbox(env, "sales").get_folder_counts()["planned"] == 1
    with pytest.raises(HubOperationError):
        env.limited.query("emails.read", {"email_key": f"scheduled-{rows[1].id}"})
    with pytest.raises(ValueError):
        action(env.service, "move_trash", f"scheduled-{rows[0].id}")
    assert not env.db.dirty


@pytest.mark.parametrize("action_name,state,direction", [
    ("mark_read", "active", "inbound"), ("mark_unread", "active", "inbound"),
    ("move_inbox", "active", "inbound"), ("move_sent", "active", "outbound"),
    ("move_trash", "trash", "inbound"), ("move_spam", "spam", "inbound"),
    ("restore", "active", "inbound"),
])
def test_all_non_deleting_mailbox_actions_share_ui_execution(env, action_name, state, direction):
    row = inbound(env)
    request = SimpleNamespace(query_params={}, state=SimpleNamespace(hub_user=env.admin))
    response = web.apply_mailbox_batch_action(request, env.db, action=action_name, keys=[key(row)], csrf_token="x")
    assert response["changed_count"] == 1
    assert row.mailbox_state == state and row.direction == direction
    if action_name == "mark_read":
        assert not row.is_unread


def test_queries_return_safe_errors_for_invalid_draft_type_and_missing_presets(env):
    row = inbound(env)
    with pytest.raises(HubOperationError):
        env.service.query("emails.drafts.read", {"draft_id": str(row.id)})
    with pytest.raises(HubOperationError):
        env.service.query("finance.presets.read", {"library_key": "sales", "preset_id": "999999"})
    with pytest.raises(ValueError):
        draft(env, customer_id="999999")


def test_unknown_folder_counts_imported_outbound_but_not_inbound_system_mail(env):
    imported = inbound(env)
    imported.direction = "outbound"
    system = inbound(env)
    system.source = "hub-task-reminder"
    env.db.commit()
    for actor in ("admin", "sales"):
        view = mailbox(env, actor).get_folder_view(folder="unassigned", unread_only=False, load_selected=False)
        assert [item.key for item in view.messages] == [key(imported)]
        assert mailbox(env, actor).get_folder_counts()["unassigned"] == len(view.messages)
