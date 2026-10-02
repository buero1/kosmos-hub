"""Account selection must only narrow permissions, across UI and agent readers."""
from datetime import UTC, datetime, timedelta
from contextlib import nullcontext
from types import SimpleNamespace
import json
import shutil
import subprocess

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse

from test_mailbox_permissions import env, grant, message
from app.api.routes import web
from app.models.customer import Customer
from app.models.hub_mailbox_account import HubMailboxAccount
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_permissions import MailboxPermissions
from app.services.hub_operations import HubOperationService
from app.services.hub_email_readers import compose_options, mailbox_accounts
from app.services.scheduled_emails import ScheduledEmailService


def scoped(env, account=0, actor="admin"):
    return HubMailboxService(db=env.db, cipher=env.cipher, actor=actor,
        public_base_url="https://hub.test", account_id=env.boxes[account].id)


@pytest.mark.parametrize("linked", [False, True])
def test_selected_account_filters_list_counts_and_single_message_even_for_admin(env, linked):
    rows = [message(env, box=i, linked=linked) for i in (0, 1)]
    if linked:
        other_customer = Customer(name="Other customer")
        env.db.add(other_customer)
        env.db.flush()
        rows[1].customer_id = other_customer.id
        rows[1].zoho_message_id = rows[0].zoho_message_id
        env.db.flush()
    key = lambda row: f"linked-{row.customer_id}-{row.id}" if linked else f"unassigned-{row.id}"
    for i in (0, 1):
        box = scoped(env, i)
        view = box.get_view(folder="inbox", unread_only=False)
        assert [item.key for item in view.messages] == [key(rows[i])]
        assert view.folder_counts["inbox"] == box.get_unread_count() == 1
        assert box.get_selected_message(folder="inbox", unread_only=False, selected_key=key(rows[1-i])) is None


def test_invoice_folder_is_available_for_both_info_mailboxes_and_aggregate(env):
    env.boxes[0].email_address = "info@kosmos-medien.de"
    env.boxes[1].email_address = "info@kosmos-websites.de"
    other_account = HubMailboxAccount(
        email_address="support@example.test",
        display_name="Support",
        username="support",
        encrypted_password=env.cipher.encrypt("secret"),
        enabled=True,
        verified_at=datetime.now(UTC),
    )
    env.db.add(other_account)
    env.db.flush()
    env.boxes.append(other_account)
    media_email = message(env, box=0)
    websites_email = message(env, box=1)
    other_email = message(env, box=2)
    media_key = f"unassigned-{media_email.id}"
    websites_key = f"unassigned-{websites_email.id}"
    other_key = f"unassigned-{other_email.id}"
    media_box = scoped(env, 0)
    websites_box = scoped(env, 1)
    other_box = scoped(env, 2)
    all_boxes = HubMailboxService(
        db=env.db,
        cipher=env.cipher,
        actor="admin",
        public_base_url="https://hub.test",
    )

    assert media_box.invoice_folder_enabled is True
    assert websites_box.invoice_folder_enabled is True
    assert all_boxes.invoice_folder_enabled is True
    assert other_box.invoice_folder_enabled is False
    assert media_box.apply_batch_action(keys=[media_key], action="move_invoices") == 1
    assert websites_box.apply_batch_action(keys=[websites_key], action="move_invoices") == 1
    assert media_email.mailbox_state == websites_email.mailbox_state == "invoices"
    assert [item.key for item in media_box.get_folder_view(folder="invoices", unread_only=False).messages] == [media_key]
    assert [item.key for item in websites_box.get_folder_view(folder="invoices", unread_only=False).messages] == [websites_key]
    assert {item.key for item in all_boxes.get_folder_view(folder="invoices", unread_only=False).messages} == {media_key, websites_key}
    assert media_box.get_folder_counts()["invoices"] == media_box.get_unread_count() == 1
    assert websites_box.get_folder_counts()["invoices"] == websites_box.get_unread_count() == 1
    assert all_boxes.get_folder_counts()["invoices"] == 2
    assert media_box.get_folder_view(folder="inbox", unread_only=False).messages == ()
    assert websites_box.get_folder_view(folder="inbox", unread_only=False).messages == ()

    with pytest.raises(ValueError, match="nur für die Info-Postfächer"):
        other_box.apply_batch_action(keys=[other_key], action="move_invoices")
    with pytest.raises(ValueError, match="Nur E-Mails aus den Info-Postfächern"):
        all_boxes.apply_batch_action(keys=[other_key], action="move_invoices")
    other_email.mailbox_state = "invoices"
    env.db.flush()
    assert {item.key for item in all_boxes.get_folder_view(folder="invoices", unread_only=False).messages} == {media_key, websites_key}
    assert all_boxes.get_folder_counts()["invoices"] == 2
    with pytest.raises(ValueError, match="nicht verfügbar"):
        other_box.get_folder_view(folder="invoices", unread_only=False)

    websites_box.apply_batch_action(keys=[websites_key], action="move_inbox")
    result = HubOperationService(db=env.db, cipher=env.cipher, actor="admin").execute(
        "emails.mailbox.move_invoices",
        {"email_keys": json.dumps([websites_key]), "account_id": str(env.boxes[1].id)},
    )
    assert result.outputs["changed_count"] == "1"
    assert websites_email.mailbox_state == "invoices"

    with pytest.raises(ValueError, match="nur für die Info-Postfächer"):
        HubOperationService(db=env.db, cipher=env.cipher, actor="admin").execute(
            "emails.mailbox.move_invoices",
            {"email_keys": json.dumps([other_key]), "account_id": str(env.boxes[2].id)},
        )


def test_account_list_only_exposes_granted_boxes_and_never_credentials(env):
    grant(env, box=1)
    gateway = HubOperationService(db=env.db, cipher=env.cipher, actor="staff")
    accounts = gateway.query("emails.accounts.list", {})["accounts"]
    assert [row["id"] for row in accounts] == [env.boxes[1].id]
    assert accounts == mailbox_accounts(gateway)
    assert all(not any(word in key for word in ("password", "secret", "username")) for row in accounts for key in row)
    for account_id in (env.boxes[0].id, 99999):
        with pytest.raises(ValueError, match="Postfach"):
            MailboxPermissions(db=env.db, actor="staff", account_id=account_id)
        with pytest.raises(ValueError):
            gateway.query("emails.list", {"account_id": str(account_id)})


def test_shared_agent_filters_and_global_navigation_badge(env):
    for i in (0, 1):
        grant(env, box=i)
        message(env, box=i)
    gateway = HubOperationService(db=env.db, cipher=env.cipher, actor="staff")
    query = {"account_id": str(env.boxes[1].id), "folder": "inbox"}
    listing = gateway.query("emails.list", query)
    assert listing["total"] == 1
    key = listing["items"][0]["email_key"]
    assert gateway.query("emails.read", {"account_id": query["account_id"], "email_key": key})["email_key"] == key
    with pytest.raises(ValueError):
        gateway.query("emails.read", {"account_id": str(env.boxes[0].id), "email_key": key})
    status = gateway.query("emails.status", {"account_id": query["account_id"]})
    assert status["folder_counts"]["inbox"] == status["account_unread_count"] == 1
    assert status["unread_count"] == 2


def test_drafts_and_planned_messages_follow_sender_membership(env):
    mailbox = HubMailboxService(db=env.db, cipher=env.cipher, actor="admin", public_base_url="https://hub.test")
    for i in (0, 1):
        mailbox.save_draft(draft_id=None, sender_email=env.boxes[i].email_address,
            recipient_email="recipient@example.test", recipient_key="", recipient_customer_id=None,
            recipient_name="", subject="Draft", content="Draft", cc_emails="", template_id="",
            reply_to_email_id="", forward_from_email_id="")
        ScheduledEmailService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test").schedule(
            actor="admin", scheduled_at=datetime.now(UTC) + timedelta(hours=1),
            sender_email=env.boxes[i].email_address, recipient_email="recipient@example.test",
            recipient_name="", subject="Planned", content="Planned", cc_emails="")
    for i in (0, 1):
        box = scoped(env, i)
        assert box.get_folder_counts()["drafts"] == box.get_folder_counts()["planned"] == 1
        assert len(box.get_folder_view(folder="drafts", unread_only=False).messages) == 1
        assert len(box.get_folder_view(folder="planned", unread_only=False).messages) == 1


def test_selected_account_defaults_sender_without_granting_new_access(env):
    for i in (0, 1):
        grant(env, box=i)
    gateway = HubOperationService(db=env.db, cipher=env.cipher, actor="staff")
    data = compose_options(gateway, env.boxes[1].id)
    assert data["default_sender_email"] == env.boxes[1].email_address
    assert gateway.query("emails.compose.options", {"account_id": str(env.boxes[1].id)})["default_sender_email"] == data["default_sender_email"]
    grant(env, subject="user", box=1, view=False)
    with pytest.raises(ValueError):
        compose_options(gateway, env.boxes[1].id)


@pytest.mark.parametrize("value", ["-1", "0", "abc", "999999999999999", "1 OR 1=1", "\u0661"])
def test_malformed_account_selector_rejected(value):
    with pytest.raises(HTTPException) as error:
        web._mailbox_account_id(SimpleNamespace(query_params={"account_id": value}))
    assert error.value.status_code == 422


@pytest.mark.parametrize("endpoint", ["mailbox_page", "mailbox_folder_list", "mailbox_folder_panel", "mailbox_selected_pane", "mailbox_status", "mailbox_compose_options"])
def test_all_native_read_paths_reject_other_users_account(env, monkeypatch, endpoint):
    grant(env)
    monkeypatch.setattr(web, "get_secret_cipher", lambda: env.cipher)
    monkeypatch.setattr(web, "SessionLocal", lambda: nullcontext(env.db))
    request = Request({"type": "http", "method": "GET", "path": "/emails", "headers": [],
        "query_string": f"account_id={env.boxes[1].id}".encode(),
        "state": {"hub_user": env.staff}})
    with pytest.raises(HTTPException) as error:
        getattr(web, endpoint)(request=request, db=env.db)
    assert error.value.status_code == 404


def test_success_redirect_retains_account_but_error_stays_inline():
    request = Request({"type": "http", "query_string": b"account_id=3", "headers": [(b"accept", b"application/json")]})
    response = web._email_compose_response(request, RedirectResponse("/emails?folder=sent&selected=unassigned-10"))
    assert b"account_id=3" in response.body
    error = web._email_compose_response(request, RedirectResponse("/emails?folder=sent"), error="Rejected")
    assert error.status_code == 400 and b"redirect_url" not in error.body


def test_frontend_account_navigation_and_cache_isolation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for navigation tests")
    result = subprocess.run([node, "tests/js/mailbox_account_selection.cjs"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
