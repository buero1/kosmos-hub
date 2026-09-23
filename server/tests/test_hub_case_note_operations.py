import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.api.routes import web
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail, CustomerZohoNote
from app.models.hub_agent import HubAgentAction, HubAgentConversation, HubAgentJob
from app.models.hub_case import HubCase
from app.models.hub_case_email_link import HubCaseEmailLink
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
from app.models.hub_user import HubUser
from app.services.hub_access_control import HubAccessControlService, permission_target
from app.services.hub_agent import HubAgentError, HubAgentService
from app.services.hub_cases import HubCaseService
from app.services.hub_case_field_catalog import HUB_CASE_FIELDS
from app.services.hub_note_catalog import note_fields
from app.services.hub_operations import HubOperationError, HubOperationService, get_operation
from app.services.zoho_crm import ZohoCrmError, ZohoCrmService


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        cipher = get_secret_cipher()
        user = HubUser(username="admin", password_hash="x", role="admin")
        customer = Customer(name="Customer", is_visible=True, zoho_id="account-1")
        hidden = Customer(name="Hidden", is_visible=True, zoho_id="account-2")
        lead = HubLead(encrypted_profile_json=cipher.encrypt('{"fields":{"company":"Lead"}}'))
        db.add_all([user, customer, hidden, lead])
        db.commit()
        monkeypatch.setattr(web, "_require_hub_admin", lambda request: user)
        monkeypatch.setattr(web, "require_csrf", lambda request, token: None)
        monkeypatch.setattr(web, "get_csrf_token", lambda request: "test")
        monkeypatch.setattr(web, "write_audit_log", lambda *args, **kwargs: None)
        monkeypatch.setattr(HubCaseService, "_now_form_value", classmethod(lambda cls: "2026-09-19T12:00"))
        calls = []

        def create_note(self, **values):
            calls.append(("create", values))
            return {"id": f"note-{len(calls)}"}

        monkeypatch.setattr(ZohoCrmService, "create_account_note", create_note)
        monkeypatch.setattr(ZohoCrmService, "update_note", lambda self, **values: calls.append(("update", values)))
        monkeypatch.setattr(ZohoCrmService, "delete_note", lambda self, **values: calls.append(("delete", values)))
        yield SimpleNamespace(db=db, cipher=cipher, user=user, customer=customer, hidden=hidden, lead=lead,
            service=HubOperationService(db=db, cipher=cipher, actor="admin"),
            cases=HubCaseService(db=db, cipher=cipher), calls=calls, monkeypatch=monkeypatch)
    engine.dispose()


def request_for(env, values):
    class Request:
        state = SimpleNamespace(hub_user=env.user)

        async def form(self):
            return FormData(values)
    return Request()


def agent_action(env, key, values, *, job=None, order=0):
    job = job or HubAgentJob(created_by_username="admin", status="ready",
        encrypted_request_json=env.cipher.encrypt('{"instruction":"Test"}'),
        encrypted_plan_json=env.cipher.encrypt('{"summary":"Test","response":"Test"}'))
    action = HubAgentAction(job=job, action_type=key, sort_order=order, status="proposed",
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"title": "Test", "details": "Test", "input": values})))
    env.db.add(action)
    env.db.flush()
    return action


def execute_agent(env, key, values):
    action = agent_action(env, key, values)
    view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor="admin")
    assert view.status == "completed", view.error
    return json.loads(env.cipher.decrypt(action.encrypted_result_json))["outputs"]


def email(env, customer=None):
    record = CustomerZohoEmail(customer_id=(customer or env.customer).id, source="hub", direction="inbound",
        encrypted_payload_json=env.cipher.encrypt('{"subject":"Request","content":"Please call"}'), encrypted_header_json="")
    env.db.add(record)
    env.db.flush()
    return f"linked-{record.customer_id}-{record.id}"


def new_case(env, **values):
    return env.service.execute("cases.create", {"case_field__case_origin": "Web", "case_field__description": "Keep me",
        "customer_id": str(env.customer.id), **values}).record_id


@pytest.mark.parametrize("mailbox", [False, True])
def test_ui_and_agent_case_create_and_update_share_defaults(env, mailbox):
    values = {"customer_id": str(env.customer.id), "case_field__description": "Request"}
    if mailbox:
        values["source_email_key"] = email(env)
    else:
        values["case_field__case_origin"] = "Web"
    route = web.create_mailbox_case if mailbox else web.create_case_page
    response = asyncio.run(route(request_for(env, values), env.db))
    assert (isinstance(response, dict) if mailbox else response.status_code == 303)
    human = env.db.scalars(select(HubCase)).one()
    if mailbox:
        values["source_email_key"] = email(env)
    outputs = execute_agent(env, "cases.create", values)
    agent = env.db.get(HubCase, int(outputs["case_id"]))
    assert env.cases._values(human) == env.cases._values(agent)
    assert human.customer_id == agent.customer_id == env.customer.id
    changes = {"case_field__status": "Abgeschlossen", "customer_id": str(env.customer.id)}
    response = asyncio.run(web.update_case_fields(human.id, request_for(env, changes), env.db))
    assert "completion_email=true" in response.headers["location"]
    outputs = execute_agent(env, "cases.update", {"case_id": str(agent.id), **changes})
    assert outputs["completed_now"] == "true"
    assert env.cases._values(human) == env.cases._values(agent)
    assert env.cases._values(agent)["description"] == "Request"


def test_case_links_unlinks_and_customer_consistency(env):
    case_id = new_case(env)
    key = email(env)
    linked = execute_agent(env, "cases.link_email", {"case_id": str(case_id), "source_email_key": key})
    again = env.service.execute("cases.link_email", {"case_id": str(case_id), "source_email_key": key})
    assert again.outputs["link_id"] == linked["link_id"]
    with pytest.raises(ValueError):
        env.service.execute("cases.update", {"case_id": str(case_id), "customer_id": str(env.hidden.id)})
    other = new_case(env)
    with pytest.raises(ValueError):
        env.service.execute("cases.unlink_email", {"case_id": str(other), "link_id": linked["link_id"]})
    response = asyncio.run(web.unlink_email_from_case(case_id, int(linked["link_id"]), request_for(env, {}), env.db))
    assert "email_link=success" in response.headers["location"]
    assert env.db.scalars(select(HubCaseEmailLink)).all() == []
    execute_agent(env, "cases.delete", {"case_id": str(case_id)})
    assert env.db.get(HubCase, case_id) is None
    assert len(env.db.scalars(select(CustomerZohoEmail)).all()) == 1


def test_duplicate_email_case_creation_is_atomic(env):
    key = email(env)
    new_case(env, source_email_key=key)
    action = agent_action(env, "cases.create", {"source_email_key": key})
    view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor="admin")
    assert view.status == "failed"
    assert len(env.db.scalars(select(HubCase)).all()) == 1


@pytest.mark.parametrize("location", ["mailbox", "customer", "detail"])
def test_case_editors_and_delete_routes_keep_their_responses(env, location):
    key = email(env)
    case_id = new_case(env, source_email_key=key)
    values = {"source_email_key": key, "customer_id": str(env.customer.id), "case_field__status": "Abgeschlossen"}
    request = request_for(env, values)
    if location == "mailbox":
        response = asyncio.run(web.update_mailbox_case(case_id, request, env.db))
        assert response["case_id"] == case_id
        response = asyncio.run(web.delete_mailbox_case(case_id, request, env.db))
        assert response == {"case_id": case_id, "source_email_key": key}
    elif location == "customer":
        response = asyncio.run(web.update_customer_case(env.customer.id, case_id, request, env.db))
        assert response["case_id"] == case_id and response["completion_email"]
        response = asyncio.run(web.delete_customer_case(env.customer.id, case_id, request, env.db))
        assert response == {"case_id": case_id}
    else:
        response = asyncio.run(web.delete_case_from_hub(case_id, request, env.db))
        assert response.headers["location"] == "/cases?deleted=true"
    assert env.db.get(HubCase, case_id) is None


def test_customer_email_link_route_uses_shared_operation(env):
    case_id = new_case(env)
    response = asyncio.run(web.link_customer_email_to_case(request_for(env, {"case_id": str(case_id), "source_email_key": email(env)}), env.db))
    assert response.status_code == 303
    assert env.db.scalars(select(HubCaseEmailLink)).one().case_id == case_id


@pytest.mark.parametrize("action", ["create", "update", "delete"])
def test_mailbox_case_routes_require_an_email_context(env, action):
    case_id = new_case(env)
    env.db.commit()
    request = request_for(env, {"customer_id": str(env.customer.id), "case_field__case_origin": "Web"})
    route = getattr(web, f"{action}_mailbox_case")
    response = asyncio.run(route(request, env.db) if action == "create" else route(case_id, request, env.db))
    assert response.status_code == 400
    assert env.db.scalars(select(HubCase)).one().id == case_id


@pytest.mark.parametrize("module", ["customers", "leads"])
def test_ui_and_agent_notes_crud_parity(env, module):
    customer = module == "customers"
    parent = env.customer if customer else env.lead
    parent_key = "customer_id" if customer else "lead_id"
    model = CustomerZohoNote if customer else HubLeadNote
    prefix = "customer_communication" if customer else "lead"
    values = {"content": "First line\nSecond line"}
    response = getattr(web, f"create_{prefix}_note")(parent.id, request_for(env, {}), env.db, title="", content=values["content"], csrf_token="")
    assert response.status_code == 303
    human = env.db.scalars(select(model)).one()
    outputs = execute_agent(env, f"{module}.notes.create", {parent_key: str(parent.id), **values})
    agent = env.db.get(model, int(outputs["note_id"]))
    assert human.encrypted_payload_json != agent.encrypted_payload_json
    assert json.loads(env.cipher.decrypt(human.encrypted_payload_json)) == json.loads(env.cipher.decrypt(agent.encrypted_payload_json))
    getattr(web, f"update_{prefix}_note")(parent.id, human.id, request_for(env, {}), env.db, title="First line", content="Edited", csrf_token="")
    execute_agent(env, f"{module}.notes.update", {parent_key: str(parent.id), "note_id": str(agent.id), "content": "Edited"})
    assert json.loads(env.cipher.decrypt(human.encrypted_payload_json)) == json.loads(env.cipher.decrypt(agent.encrypted_payload_json))
    getattr(web, f"delete_{prefix}_note")(parent.id, human.id, request_for(env, {}), env.db, csrf_token="")
    execute_agent(env, f"{module}.notes.delete", {parent_key: str(parent.id), "note_id": str(agent.id)})
    assert env.db.scalars(select(model)).all() == []
    assert [action for action, _ in env.calls] == (["create", "create", "update", "update", "delete", "delete"] if customer else [])


@pytest.mark.parametrize("module", ["customers", "leads"])
def test_notes_reject_wrong_parent_and_blank_patch(env, module):
    key = "customer_id" if module == "customers" else "lead_id"
    parent = env.customer.id if module == "customers" else env.lead.id
    result = env.service.execute(f"{module}.notes.create", {key: str(parent), "content": "Retain"})
    for values in ({key: "99999"}, {"title": ""}, {"content": ""}):
        with pytest.raises(ValueError):
            env.service.execute(f"{module}.notes.update", {key: str(parent), "note_id": str(result.record_id), **values})


def test_zoho_failure_retains_note_and_agent_shows_warning(env):
    def fail(*args, **kwargs):
        raise ZohoCrmError("Not connected")
    env.monkeypatch.setattr(ZohoCrmService, "create_account_note", fail)
    action = agent_action(env, "customers.notes.create", {"customer_id": str(env.customer.id), "content": "Retain"})
    service = HubAgentService(db=env.db, cipher=env.cipher)
    view = service.execute_action(action_id=action.id, actor="admin")
    assert view.status == "completed"
    assert any("konnte aber noch nicht" in line for line in view.preview_lines)
    note = env.db.scalars(select(CustomerZohoNote)).one()
    assert note.sync_status == "failed"
    outputs = json.loads(env.cipher.decrypt(action.encrypted_result_json))["outputs"]
    assert outputs["sync_status"] == "failed" and outputs["note_id"] == str(note.id)
    update = agent_action(env, "customers.notes.update", {"customer_id": str(env.customer.id), "note_id": str(note.id), "content": "Do not save"})
    assert service.execute_action(action_id=update.id, actor="admin").status == "failed"
    assert "Retain" in env.cipher.decrypt(note.encrypted_payload_json)


@pytest.mark.parametrize("operation", ["cases.create", "cases.update", "cases.delete", "cases.link_email", "cases.unlink_email",
    "customers.notes.create", "customers.notes.update", "customers.notes.delete", "leads.notes.create", "leads.notes.update", "leads.notes.delete"])
def test_permissions_and_record_scope_are_enforced_for_every_operation(env, operation):
    case_id = new_case(env, customer_id=str(env.hidden.id))
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    sales = HubUser(username="sales", password_hash="x", role="sales")
    inactive = HubUser(username="inactive", password_hash="x", role="admin", is_active=False)
    env.db.add_all([sales, inactive])
    env.db.flush()
    access.assign_created_record(user=sales, module_key="customers", record_id=env.customer.id)
    values = {"case_id": str(case_id), "customer_id": str(env.hidden.id), "lead_id": str(env.lead.id),
        "note_id": "1", "link_id": "1", "content": "No access", "case_field__case_origin": "Web"}
    for actor in ("sales", "inactive", "missing"):
        with pytest.raises(ValueError):
            HubOperationService(db=env.db, cipher=env.cipher, actor=actor).execute(operation, values)
    assert env.calls == []


def test_inaccessible_email_cannot_be_linked_to_visible_case(env):
    case_id = new_case(env)
    key = email(env, env.hidden)
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    user = HubUser(username="sales", password_hash="x", role="sales")
    env.db.add(user)
    env.db.flush()
    access.assign_created_record(user=user, module_key="customers", record_id=env.customer.id)
    with pytest.raises(HubOperationError):
        HubOperationService(db=env.db, cipher=env.cipher, actor="sales").execute("cases.link_email", {"case_id": str(case_id), "source_email_key": key})


def test_catalog_drives_forms_contracts_and_validation(env):
    context = web._case_create_context(request_for(env, {}), env.db)
    contract = get_operation("cases.create").input_contract()
    for field in HUB_CASE_FIELDS:
        if field.read_only or field.key == "customer_name":
            continue
        assert contract[f"case_field__{field.key}"]["required"] == field.required
    assert context["submitted_values"]["case_field__status"] == contract["case_field__status"]["default"] == "Neu"
    assert get_operation("cases.create").apply_defaults({"source_email_key": "linked-1-1"})["case_field__case_origin"] == "E-Mail"
    for field in note_fields(creating=True):
        assert get_operation("leads.notes.create").input_contract()[field.name]["required"] == field.required
        assert web.templates.env.globals["note_field_catalog"](True)[field.name] == field
    for alias in ("create_case_from_email", "link_email_to_case", "create_customer_note"):
        assert get_operation(alias) is None
        with pytest.raises(HubAgentError):
            HubAgentService._normalize_action({"action_type": alias, "title": "Old", "details": "", "input": {}})


def test_agent_respects_note_length_from_module_without_truncation(env):
    values = {"lead_id": str(env.lead.id), "content": "a" * 30_000}
    raw = {"action_type": "leads.notes.create", "title": "Note", "details": "Note", "input": values}
    normalized = HubAgentService._normalize_action(raw)
    result = env.service.execute("leads.notes.create", normalized["input"])
    note = env.db.get(HubLeadNote, result.record_id)
    assert len(json.loads(env.cipher.decrypt(note.encrypted_payload_json))["Note_Content"]) == 30_000
    raw["input"]["content"] += "a"
    with pytest.raises(HubAgentError):
        HubAgentService._normalize_action(raw)


def test_cases_and_notes_contexts_are_fresh_and_recheck_rights(env):
    case_id = new_case(env)
    note = env.service.execute("customers.notes.create", {"customer_id": str(env.customer.id), "content": "Before"})
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    chat = agent.start_conversation(actor="admin")
    for kind, record_id in (("case", case_id), ("note", note.record_id)):
        agent.add_context(actor="admin", conversation_id=chat.conversation_id, resource_type=kind, resource_key=str(record_id))
    env.service.execute("cases.update", {"case_id": str(case_id), "case_field__description": "Updated case"})
    env.service.execute("customers.notes.update", {"customer_id": str(env.customer.id), "note_id": str(note.record_id), "content": "Updated note"})
    conversation = env.db.get(HubAgentConversation, chat.conversation_id)
    prompt = "\n".join(agent._conversation_prompt_contexts(conversation, exclude_email_key=""))
    assert "Updated case" in prompt and "Updated note" in prompt and "Notiz-ID:" in prompt
    env.user.is_active = False
    assert agent._conversation_prompt_contexts(conversation, exclude_email_key="") == ()


def test_case_and_note_results_compose_without_a_special_workflow(env):
    first = agent_action(env, "cases.create", {"customer_id": str(env.customer.id), "case_field__case_origin": "Web"})
    second = agent_action(env, "customers.notes.create", {"customer_id": "{{action.1.customer_id}}", "content": "A new case was created"}, job=first.job, order=1)
    third = agent_action(env, "customers.notes.update", {"customer_id": "{{action.2.customer_id}}", "note_id": "{{action.2.note_id}}", "title": "Case update"}, job=first.job, order=2)
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    for action in (first, second, third):
        result = agent.execute_action(action_id=action.id, actor="admin")
        assert result.status == "completed", result.error
    note = env.db.scalars(select(CustomerZohoNote)).one()
    assert note.customer_id == env.customer.id
    assert "Case update" in env.cipher.decrypt(note.encrypted_payload_json)


def test_lead_context_loads_current_notes_on_demand(env):
    result = env.service.execute("leads.notes.create", {"lead_id": str(env.lead.id), "content": "Current note"})
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    prompt = agent._context_snapshot(resource_type="lead", resource_key=str(env.lead.id))["prompt"]
    assert "Current note" not in prompt and "nachladen" in prompt
    page = env.service.query("leads.notes.list", {"lead_id": str(env.lead.id)})
    assert page["items"][0]["note_id"] == str(result.record_id)
    assert page["items"][0]["content"] == "Current note"


@pytest.mark.parametrize("module", ("customers", "leads"))
def test_lazy_notes_are_paginated_complete_and_authorized(env, module):
    parent = env.customer if module == "customers" else env.lead
    identity = "customer_id" if module == "customers" else "lead_id"
    values = {identity: str(parent.id)}
    for index in range(11):
        result = env.service.execute(f"{module}.notes.create", {**values, "content": f"Note {index} " + "x" * 7000})
    page = env.service.query(f"{module}.notes.list", values)
    assert len(page["items"]) == 10 and page["next_offset"] == "10"
    assert len(page["items"][0]["content"]) == 600
    assert page["items"][0]["next_text_offset"] == "600"
    assert len(env.service.query(f"{module}.notes.list", {**values, "offset": "10"})["items"]) == 1
    first = env.service.query(f"{module}.notes.read", {**values, "note_id": str(result.record_id)})["items"][0]
    second = env.service.query(f"{module}.notes.read", {**values, "note_id": str(result.record_id), "text_offset": first["next_text_offset"]})["items"][0]
    assert first["content"] + second["content"] == "Note 10 " + "x" * 7000
    assert second["next_text_offset"] is None
    calls_before = list(env.calls)
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    user = HubUser(username="limited", password_hash="x", role="sales")
    env.db.add(user)
    env.db.flush()
    gateway = HubOperationService(db=env.db, cipher=env.cipher, actor=user.username)
    with pytest.raises(HubOperationError):
        gateway.query(f"{module}.notes.list", values)
    access.assign_created_record(user=user, module_key=module, record_id=parent.id)
    assert gateway.query(f"{module}.notes.list", values)["items"]
    user.is_active = False
    with pytest.raises(HubOperationError):
        gateway.query(f"{module}.notes.read", {**values, "note_id": str(result.record_id)})
    assert env.calls == calls_before


def test_generic_email_context_binding_does_not_accept_invented_keys():
    raw = {"summary": "Link", "response": "Link", "actions": [{"action_type": "cases.link_email", "title": "Link", "details": "Link", "input": {"case_id": "1"}}]}
    result = HubAgentService._normalize_plan(raw, allowed_email_keys=("linked-1-1",))
    assert result["actions"][0]["input"]["source_email_key"] == "linked-1-1"
    raw["actions"][0]["input"]["source_email_key"] = "linked-999-999"
    with pytest.raises(HubAgentError):
        HubAgentService._normalize_plan(raw, allowed_email_keys=("linked-1-1",))


def test_all_record_write_routes_use_gateway_and_permissions_match():
    tree = ast.parse(Path("app/api/routes/web.py").read_text(encoding="utf-8"))
    case_routes = {"create_case_page", "link_customer_email_to_case", "unlink_email_from_case", "update_case_fields", "delete_case_from_hub",
        "create_mailbox_case", "update_mailbox_case", "delete_mailbox_case", "update_customer_case", "delete_customer_case"}
    note_routes = {f"{action}_{prefix}_note" for action in ("create", "update", "delete") for prefix in ("lead", "customer_communication")}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in case_routes | note_routes:
            source = ast.unparse(node)
            assert ("_execute_case_operation" if node.name in case_routes else "_execute_note_operation") in source
            assert not any(f".{name}(" in source for name in ("create_case", "update_case", "delete_case", "create_note", "update_note", "delete_note"))
    assert permission_target("/emails/cases", "POST") == ("cases", "create")
    assert permission_target("/cases/1/email-links/2/delete", "POST") == ("cases", "edit")
