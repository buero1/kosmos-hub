import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.api.routes import web
from app.models.customer_contact import CustomerContact
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.zoho_email_template import ZohoEmailTemplate
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_operations import HubArtifact, HubOperationError, HubOperationService, agent_operations
from app.services.hub_agent import HubAgentService
from app.services.hub_access_control import permission_target
from test_hub_agent import _add_action
from test_hub_mailbox_operations import env, inbound, key, mailbox


def template(env, template_id="source", **values):
    record = ZohoEmailTemplate(zoho_template_id=template_id, module="Accounts", zoho_synced_at=datetime.now(UTC),
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"name": "Example", "subject": "Hello",
            "content": "<p>Template body</p>", "folder_name": "Customers", "hub_context_module": "customers", **values})))
    env.db.add(record)
    env.db.commit()
    return record


def payload(env, record):
    return json.loads(env.cipher.decrypt(record.encrypted_payload_json))


def draft_payload(env, result):
    return payload(env, env.db.get(HubMailboxEmail, result.record_id))


def contact(env, customer, name="Alice", email="alice@example.test"):
    row = CustomerContact(customer_id=customer.id, encrypted_profile_json=env.cipher.encrypt(json.dumps(
        {"fields": {"Name": name, "Vorname": name, "Nachname": "Example", "E-Mail": email}})))
    env.db.add(row)
    env.db.commit()
    return row


def test_template_partial_update_keeps_markup_styles_and_local_clone_is_independent(env):
    row = template(env, compiler_stylesheet=".keep {color: red;}")
    before = payload(env, row)
    result = env.service.execute("emails.templates.update", {"template_id": "source", "name": "Renamed"})
    after = payload(env, row)
    assert after["content"] == before["content"] and after["compiler_stylesheet"] == before["compiler_stylesheet"]
    assert after["subject"] == "Hello" and after["name"] == "Renamed"
    assert result.outputs["template_id"] == "source"
    copied = env.service.execute("emails.templates.clone", {"template_id": "source", "name": "Copy"})
    assert copied.outputs["template_id"].startswith("hub-template-")
    env.service.execute("emails.templates.delete", {"template_ids": json.dumps([copied.outputs["template_id"]])})
    assert env.db.get(ZohoEmailTemplate, copied.record_id) is None
    env.service.execute("emails.templates.delete", {"template_ids": '["source"]'})
    assert env.db.get(ZohoEmailTemplate, row.id).is_active is False
    assert payload(env, row)["content"] == before["content"]


def test_template_batches_preflight_all_ids_and_folder_delete_checks_contents(env):
    row = template(env)
    folder = env.service.execute("emails.templates.folders.create", {"name": "Archive"})
    env.db.commit()
    for action in ("move", "delete"):
        values = {"template_ids": '["source", "missing"]'}
        if action == "move":
            values["folder_name"] = "Archive"
        with pytest.raises(HubOperationError):
            env.service.execute(f"emails.templates.{action}", values)
        assert row.is_active and payload(env, row)["folder_name"] == "Customers"
    env.service.execute("emails.templates.move", {"template_ids": '["source"]', "folder_name": "Archive"})
    with pytest.raises(HubOperationError):
        env.service.execute("emails.templates.folders.delete", {"folder_id": str(folder.record_id)})
    env.service.execute("emails.templates.delete", {"template_ids": '["source"]'})
    env.service.execute("emails.templates.folders.delete", {"folder_id": str(folder.record_id)})
    assert not env.service.query("emails.templates.folders", {})["items"]


def test_template_queries_are_paginated_readonly_and_role_checked(env):
    for number in range(27):
        template(env, template_id=f"source-{number}", content="<p>" + "x" * 13000 + "</p>")
    before = env.db.query(ZohoEmailTemplate).count()
    first = env.service.query("emails.templates.list", {"folder_name": "Customers"})
    assert len(first["items"]) == 25 and first["total"] == 27
    assert len(env.service.query("emails.templates.list", {"offset": first["next_offset"]})["items"]) == 2
    source = ""
    offset = "0"
    while offset:
        page = env.service.query("emails.templates.read", {"template_id": "source-0", "text_offset": offset})
        source += page["content"]
        offset = page["next_text_offset"]
    assert source == "<p>" + "x" * 13000 + "</p>"
    assert env.db.query(ZohoEmailTemplate).count() == before
    assert not env.db.dirty and not env.db.new
    viewer = HubOperationService(db=env.db, cipher=env.cipher, actor="viewer")
    with pytest.raises(HubOperationError):
        viewer.execute("emails.templates.update", {"template_id": "source-0", "name": "Not allowed"})


def test_template_ui_and_operation_use_same_update_and_customer_rendering(env):
    template(env, subject="Offer ${Accounts.Account_Name}")
    contact(env, env.own)
    response = web.update_email_template(template_id="source", request=None, db=env.db, name="Changed", subject="Offer ${Accounts.Account_Name}",
        content="<p>${Contacts.First_Name}</p>", context_module="customers", folder_name="Customers", csrf_token="x")
    assert response.status_code == 303
    rendered = env.service.query("emails.templates.render", {"template_id": "source", "customer_id": str(env.own.id)})
    ui = web.load_customer_communication_email_template(env.own.id, "source", request=None, db=env.db)
    assert all(ui[name] == rendered[name] for name in ("subject", "content", "unresolved_placeholders"))
    assert rendered["subject"] == "Offer Visible" and "Alice" in rendered["content"]
    assert rendered["unresolved_placeholders"] == ()
    with pytest.raises(HubOperationError):
        env.limited.query("emails.templates.render", {"template_id": "source", "customer_id": str(env.hidden.id)})


def test_template_draft_chooses_actual_recipient_and_rejects_mismatch_or_missing_values(env):
    contact(env, env.own, "Alice", "alice@example.test")
    contact(env, env.own, "Bob", "bob@example.test")
    template(env, content="<p>${Contacts.First_Name}</p>")
    result = env.service.execute("emails.drafts.from_template", {"template_id": "source", "customer_id": str(env.own.id), "recipient_email": "bob@example.test"})
    data = draft_payload(env, result)
    assert "Bob" in data["content"] and "Alice" not in data["content"]
    assert data["recipient_customer_id"] == env.own.id
    recipients = mailbox(env).communications.list_recipients(customer_id=env.own.id)
    bob_key = next(item.key for item in recipients if item.email == "bob@example.test")
    with pytest.raises(HubOperationError, match="stimmen nicht"):
        env.service.execute("emails.drafts.from_template", {"template_id": "source", "customer_id": str(env.own.id), "recipient_key": bob_key, "recipient_email": "alice@example.test"})
    with pytest.raises(HubOperationError, match="stimmen nicht"):
        env.service.execute("emails.drafts.from_template", {"template_id": "source", "customer_id": str(env.own.id), "recipient_email": "other@example.test"})
    with pytest.raises(HubOperationError, match="Werte"):
        env.service.execute("emails.drafts.from_template", {"template_id": "source", "recipient_email": "bob@example.test"})
    assert env.db.query(HubMailboxEmail).count() == 1


@pytest.mark.parametrize("action", ["reply", "reply_all", "forward"])
def test_mailbox_composition_matches_ui_and_draft_remains_unsent(env, action):
    source = inbound(env, sender="alice@example.test", to=[{"email": "sender@example.test"}], cc=[{"email": "copy@example.test"}])
    preview = env.service.query("emails.compose.preview", {"email_key": key(source), "action": action})
    ui = web.mailbox_unassigned_email_compose_context(source.id, request=None, db=env.db, action=action)
    assert all(ui[name] == preview[name] for name in ("recipient_email", "subject", "content", "cc_emails"))
    values = {"email_key": key(source), "content": "<p>My answer</p>", "sender_email": "sender@example.test"}
    if action == "forward":
        values["recipient_email"] = "forward@example.test"
    result = env.service.execute(f"emails.drafts.{action}", values)
    draft = env.db.get(HubMailboxEmail, result.record_id)
    data = payload(env, draft)
    assert draft.mailbox_state == "draft" and source.is_unread
    assert data["content"].endswith(preview["content"]) and "My answer" in data["content"]
    assert data["subject"] == preview["subject"]
    assert data["recipient_email"] == ("forward@example.test" if action == "forward" else "alice@example.test")
    assert data["cc_emails"] == ("copy@example.test" if action == "reply_all" else "")
    assert not data.get("forward_from_email_id")


def test_forward_copies_files_once_and_reply_never_copies_them(env):
    uploader = HubOperationService(db=env.db, cipher=env.cipher, actor="admin", input_files=(
        HubArtifact(filename="source.pdf", content=b"%PDF sample", content_type="application/pdf"),))
    original = uploader.execute("emails.drafts.save", {"subject": "File", "content": "<p>Original</p>"})
    row = env.db.get(HubMailboxEmail, original.record_id)
    data = payload(env, row)
    data["sender"] = "original@example.test"
    row.encrypted_payload_json = env.cipher.encrypt(json.dumps(data))
    row.source, row.direction, row.mailbox_state = "mittwald-imap", "inbound", "active"
    env.db.commit()
    forwarded = env.service.execute("emails.drafts.forward", {"email_key": key(row), "recipient_email": "target@example.test", "content": "Please read"})
    env.db.commit()
    context = mailbox(env).get_draft_compose_context(draft_id=forwarded.record_id)
    assert len(context["attachments"]) == 1 and not context["forward_from_email_id"]
    files = mailbox(env).draft_attachments(draft_id=forwarded.record_id)
    assert [(item.filename, item.content) for item in files] == [("source.pdf", b"%PDF sample")]
    replied = env.service.execute("emails.drafts.reply", {"email_key": key(row), "content": "Thanks"})
    assert not mailbox(env).draft_attachments(draft_id=replied.record_id)


def test_missing_forwarded_attachment_fails_before_creating_draft(env):
    source = inbound(env, attachments=[{"id": "missing", "file_name": "missing.pdf"}])
    with pytest.raises(HubOperationError):
        env.service.execute("emails.drafts.forward", {"email_key": key(source), "recipient_email": "other@example.test", "content": "See attachment"})
    assert not env.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.mailbox_state == "draft"))


def test_linked_preview_is_readonly_and_hidden_sources_are_rejected_before_fetch(env, monkeypatch):
    contact(env, env.own)
    source = inbound(env, customer=env.own, message_id="message", sender="alice@example.test", **{"from": {"email": "alice@example.test"}})
    hidden = inbound(env, customer=env.hidden, message_id="hidden")
    monkeypatch.setattr(CustomerCommunicationService, "_load_email_content_for_email", lambda *args, **kwargs: pytest.fail("Query must not fetch or write"))
    before = source.encrypted_payload_json
    result = env.limited.query("emails.compose.preview", {"email_key": key(source), "action": "reply"})
    assert result["recipient_email"] == "alice@example.test" and result["customer_id"] == env.own.id
    assert source.is_unread and source.encrypted_payload_json == before and not env.db.dirty
    for operation in ("preview", "reply"):
        with pytest.raises(HubOperationError):
            if operation == "preview":
                env.limited.query("emails.compose.preview", {"email_key": key(hidden), "action": "reply"})
            else:
                env.limited.execute("emails.drafts.reply", {"email_key": key(hidden), "content": "No access"})
    data = payload(env, source)
    del data["content"]
    source.encrypted_payload_json = env.cipher.encrypt(json.dumps(data))
    env.db.commit()
    with pytest.raises(HubOperationError, match="lokal|laden"):
        env.service.query("emails.compose.preview", {"email_key": key(source), "action": "reply"})


def test_new_operations_discovered_without_enabling_send(env):
    operations = {item.key for item in agent_operations()}
    assert operations >= {"emails.drafts.reply", "emails.drafts.reply_all", "emails.drafts.forward", "emails.drafts.from_template",
        "emails.templates.update", "emails.templates.clone", "emails.templates.move", "emails.templates.delete",
        "emails.templates.folders.create", "emails.templates.folders.delete"}
    assert not operations & {"emails.send", "emails.schedule", "emails.drafts.save"}


def test_template_library_web_routes_delegate_all_writes(env):
    from urllib.parse import parse_qs, urlsplit

    template(env)
    assert web.create_email_template_folder(None, env.db, name="Target", csrf_token="x").status_code == 303
    folder_id = env.service.query("emails.templates.folders", {})["items"][0]["folder_id"]
    response = web.clone_email_template("source", None, env.db, name="Copy", csrf_token="x")
    copied_id = parse_qs(urlsplit(response.headers["location"]).query)["template"][0]
    response = web.move_email_templates(None, env.db, template_ids=[copied_id], folder_name="Target", csrf_token="x")
    assert parse_qs(urlsplit(response.headers["location"]).query)["state"] == ["success"]
    response = web.delete_email_template_folder(int(folder_id), None, env.db, csrf_token="x")
    assert parse_qs(urlsplit(response.headers["location"]).query)["state"] == ["error"]
    response = web.delete_email_template(copied_id, None, env.db, confirmation="loeschen", csrf_token="x")
    assert parse_qs(urlsplit(response.headers["location"]).query)["state"] == ["success"]
    response = web.delete_email_template_folder(int(folder_id), None, env.db, csrf_token="x")
    assert parse_qs(urlsplit(response.headers["location"]).query)["state"] == ["success"]
    response = web.delete_email_templates(None, env.db, template_ids=["source"], confirmation="confirmed", csrf_token="x")
    assert parse_qs(urlsplit(response.headers["location"]).query)["state"] == ["success"]
    assert env.service.query("emails.templates.list", {})["total"] == 0


@pytest.mark.parametrize("path, permission", [
    ("/email-template-folders", "create"), ("/email-template-folders/1/delete", "delete"),
    ("/email-templates/source/clone", "create"), ("/email-templates/source", "edit"),
    ("/email-templates/bulk/move", "edit"), ("/email-templates/bulk/delete", "delete"),
    ("/email-templates/source/delete", "delete"),
])
def test_template_http_permissions_match_shared_operations(path, permission):
    assert permission_target(path, "POST") == ("emails", permission)


@pytest.mark.parametrize("action", ["reply", "reply_all", "forward", "from_template"])
def test_agent_dispatches_registered_draft_actions_only_after_approval(env, action):
    if action == "from_template":
        template(env)
        values = {"template_id": "source", "recipient_email": "alice@example.test"}
    else:
        original = inbound(env, sender="alice@example.test")
        values = {"email_key": key(original), "content": "Thank you"}
        if action == "forward":
            values["recipient_email"] = "bob@example.test"
    proposal = _add_action(env.db, env.cipher, action_type=f"emails.drafts.{action}", input_values=values)
    assert not env.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.mailbox_state == "draft"))
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    result = agent.execute_action(action_id=proposal.id, actor="hub-admin")
    assert result.status == "completed" and "folder=drafts" in result.result_href
    assert env.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.mailbox_state == "draft"))
    assert not env.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.direction == "outbound", HubMailboxEmail.mailbox_state != "draft"))
