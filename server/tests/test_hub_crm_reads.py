from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from app.api.routes import web
from app.models.customer_activity import CustomerCallActivity, CustomerTaskActivity, CustomerMeetingActivity
from app.models.hub_case import HubCase
from app.models.hub_case_email_link import HubCaseEmailLink
from app.services.hub_crm_readers import HubCrmReadService
from app.services.hub_cases import HubCaseService
from app.services.hub_operations import HubOperationError
from test_hub_email_composition import contact
from test_hub_mailbox_operations import env, inbound, key


def case(env, customer, **fields):
    row = HubCase(customer_id=customer.id, encrypted_fields_json=env.cipher.encrypt(json.dumps({"status": "Offen", "description": "Example case", **fields})))
    env.db.add(row)
    env.db.commit()
    return row


def activity(env, customer, kind, **values):
    model = {"task": CustomerTaskActivity, "call": CustomerCallActivity, "meeting": CustomerMeetingActivity}[kind]
    dates = {"due_at": datetime(2026, 8, 1, 9)} if kind == "task" else {"starts_at": datetime(2026, 8, 1, 9), "ends_at": datetime(2026, 8, 1, 10)}
    row = model(customer_id=customer.id, name="Example", status="planned", assignee_user_id=env.sales.id, **dates, **values)
    env.db.add(row)
    env.db.commit()
    return row


def test_contacts_lists_and_details_use_same_authorized_ui_reader(env, monkeypatch):
    own = contact(env, env.own)
    hidden = contact(env, env.hidden, "Secret")
    result = env.limited.query("contacts.list", {})
    reader = HubCrmReadService(db=env.db, cipher=env.cipher, actor="sales")
    assert result["total"] == 1
    assert result["items"][0]["contact_id"] == str(own.id)
    assert [entry.contact.id for entry in reader.contact_entries()] == [own.id]
    detail = env.limited.query("contacts.read", {"contact_id": str(own.id)})
    assert detail["name"] == reader.contact_detail(own.id).name
    with pytest.raises(HubOperationError):
        env.limited.query("contacts.read", {"contact_id": str(hidden.id)})
    assert env.limited.query("contacts.list", {"query": "Secret"})["total"] == 0
    assert not env.db.dirty and not env.db.new


def test_case_email_links_do_not_disclose_hidden_parent_messages(env):
    row = case(env, env.own, description="x" * 14000)
    hidden = case(env, env.hidden)
    visible_mail = inbound(env, customer=env.own)
    hidden_mail = inbound(env, customer=env.hidden)
    env.db.add_all([HubCaseEmailLink(case_id=row.id, customer_email_id=mail.id) for mail in (visible_mail, hidden_mail)])
    env.db.commit()
    result = env.limited.query("cases.read", {"case_id": str(row.id)})
    assert [item["email_key"] for item in result["email_links"]] == [key(visible_mail)]
    assert env.limited.query("cases.list", {})["total"] == 1
    assert env.limited.query("cases.list", {"query": "xxxxxxxxxx"})["total"] == 1
    assert env.limited.query("cases.list", {"query": "Example case"})["total"] == 0
    text, offset = "", "0"
    while offset:
        page = env.limited.query("cases.read", {"case_id": str(row.id), "field": "description", "text_offset": offset})["fields"][0]
        text += page["value"]
        offset = page["next_text_offset"]
    assert text == "x" * 14000
    with pytest.raises(HubOperationError):
        env.limited.query("cases.read", {"case_id": str(hidden.id)})
    assert not env.db.dirty and visible_mail.is_unread and hidden_mail.is_unread


@pytest.mark.parametrize("kind", ["call", "task", "meeting"])
def test_activity_reads_filter_scope_and_never_complete_elapsed_events(env, kind):
    own = activity(env, env.own, kind, description="d" * 13000)
    hidden = activity(env, env.hidden, kind)
    result = env.limited.query(f"activities.{kind}s.list", {"query": "Example"})
    assert result["total"] == 1 and result["items"][0]["activity_id"] == str(own.id)
    assert result["items"][0]["scheduled_at"] == "2026-08-01T11:00:00+02:00"
    text, offset = "", "0"
    while offset:
        page = env.limited.query(f"activities.{kind}s.read", {"activity_id": str(own.id), "field": "description", "text_offset": offset})["fields"][0]
        text += page["value"]
        offset = page["next_text_offset"]
    assert text == "d" * 13000
    with pytest.raises(HubOperationError):
        env.limited.query(f"activities.{kind}s.read", {"activity_id": str(hidden.id)})
    assert own.status == "planned" and hidden.status == "planned" and not env.db.dirty and not env.db.new


def test_pagination_happens_after_access_filtering(env):
    for index in range(27):
        contact(env, env.own, str(index), f"person{index}@example.test")
        contact(env, env.hidden, f"hidden{index}", f"secret{index}@example.test")
    first = env.limited.query("contacts.list", {})
    second = env.limited.query("contacts.list", {"offset": first["next_offset"]})
    assert first["total"] == 27 and len(first["items"]) == 25 and len(second["items"]) == 2
    with pytest.raises(HubOperationError):
        env.limited.query("contacts.list", {"offset": "-1"})


def test_case_detail_customer_selector_does_not_leak_hidden_customers(env, monkeypatch):
    row = case(env, env.own)
    monkeypatch.setattr(web, "get_csrf_token", lambda request: "test")
    detail = HubCrmReadService(db=env.db, cipher=env.cipher, actor="sales").case_detail(row.id)
    context = web._case_detail_context(SimpleNamespace(state=SimpleNamespace(hub_user=env.sales)),
        service=HubCaseService(db=env.db, cipher=env.cipher), detail=detail,
        fields="", fields_message="", layout="", layout_message="", email_link="", email_link_message="")
    assert [customer.id for customer in context["customers"]] == [env.own.id]


@pytest.mark.parametrize("kind", ["contacts", "cases", "activities.calls", "activities.tasks", "activities.meetings"])
def test_invalid_ids_and_unknown_fields_are_safe_errors(env, kind):
    identity = {"contacts": "contact_id", "cases": "case_id"}.get(kind, "activity_id")
    for value in ("", "abc", "-1", "999999"):
        with pytest.raises(HubOperationError):
            env.service.query(kind + ".read", {identity: value})
