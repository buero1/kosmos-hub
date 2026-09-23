import ast
import asyncio
import copy
import json
from datetime import datetime, timedelta
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
from app.models.customer_activity import CustomerCallActivity
from app.models.customer_contact import CustomerContact
from app.models.hub_case import HubCase
from app.models.hub_lead import HubLead
from app.models.hub_user import HubUser
from app.services.ai_models import DEFAULT_OPENAI_MODEL
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_agent import HubAgentError, HubAgentService
from app.services.hub_customer_field_catalog import customer_create_fields
from app.services.hub_global_search import HubGlobalSearchService
from app.services.hub_leads import HubLeadService
from app.services.hub_operations import HubOperationError, HubOperationService, get_operation, hub_queries
from app.services.zoho_crm import ZohoCrmService


def tool_output(payload):
    output = payload["input"][-1]["output"]
    if isinstance(output, list):
        output = "".join(block["text"] for block in output if block["type"] == "input_text")
    return json.loads(output)


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        cipher = get_secret_cipher()
        user = HubUser(username="admin", password_hash="x", role="admin")
        sales = HubUser(username="sales", password_hash="x", role="sales")
        db.add_all([user, sales])
        access = HubAccessControlService(db=db)
        access.ensure_defaults()
        db.commit()
        monkeypatch.setattr(web, "_require_hub_admin", lambda request: user)
        monkeypatch.setattr(web, "require_csrf", lambda request, token: None)
        monkeypatch.setattr(web, "get_csrf_token", lambda request: "test")
        monkeypatch.setattr(web, "write_audit_log", lambda *args, **kwargs: None)
        yield SimpleNamespace(db=db, cipher=cipher, user=user, sales=sales, access=access,
            service=HubOperationService(db=db, cipher=cipher, actor="admin"), monkeypatch=monkeypatch)
    engine.dispose()


def request(env, values):
    class Request:
        state = SimpleNamespace(hub_user=env.user)

        async def form(self):
            return FormData(values)
    return Request()


def lead_profile(env, lead_id):
    return json.loads(env.cipher.decrypt(env.db.get(HubLead, lead_id).encrypted_profile_json))


@pytest.mark.parametrize("kind", ["customer", "lead"])
def test_forms_and_operations_share_creation_and_defaults(env, kind):
    if kind == "customer":
        fields = {"customer_name": "Shared example", "phone": "0123456"}
        route = web.create_customer_page
        model = Customer
    else:
        fields = {"company": "Shared example", "email": "test@example.test"}
        route = web.create_lead_page
        model = HubLead
    response = asyncio.run(route(request(env, {f"{kind}_field__{key}": value for key, value in fields.items()}), env.db))
    direct = env.service.execute(f"{kind}s.create", fields)
    ui_record = env.db.get(model, int(response.headers["location"].rsplit("/", 1)[-1]))
    direct_record = env.db.get(model, direct.record_id)
    assert json.loads(env.cipher.decrypt(ui_record.encrypted_profile_json)) == json.loads(env.cipher.decrypt(direct_record.encrypted_profile_json))
    contract = get_operation(f"{kind}s.create").input_contract()
    assert contract["account_status" if kind == "customer" else "lead_status"]["default"] == ("Neu" if kind == "customer" else "Lead erstellt")


def test_customer_catalog_drives_form_and_operation(env):
    context = web._hub_customer_create_context(request(env, {}), env.db)
    contract = get_operation("customers.create").input_contract()
    for field in customer_create_fields():
        assert context["create_fields"][field.key] == field
        assert contract[field.key]["required"] == field.required
        assert contract[field.key]["max_length"] == field.max_length


def test_customer_partial_update_preserves_other_fields(env):
    created = env.service.execute("customers.create", {"customer_name": "Example", "phone": "old", "billing_city": "Berlin"})
    env.service.execute("customers.update", {"customer_id": str(created.record_id), "phone": "new"})
    data = env.service.query("customers.read", {"customer_id": str(created.record_id)})
    fields = {f["key"]: f["value"] for f in data["fields"]}
    assert fields["phone"] == "new"
    assert fields["billing_city"] == "Berlin"
    assert fields["customer_name"] == "Example"
    env.service.execute("customers.update", {"customer_id": str(created.record_id), "phone": ""})
    with pytest.raises(ValueError):
        env.service.execute("customers.update", {"customer_id": str(created.record_id), "made_up": "x"})


def test_lead_partial_update_retains_multiselect_and_repeater_then_applies_workflows(env):
    created = env.service.execute("leads.create", {"company": "Example", "email_status": '["Geöffnet"]',
        "appointment_at": (datetime.now() + timedelta(days=10)).isoformat(timespec="minutes"),
        "subform_values": json.dumps({"lead_subform__lead_results__new__lead_modified_by": "Test"})})
    before = lead_profile(env, created.record_id)
    env.service.execute("leads.update", {"lead_id": str(created.record_id), "phone": "0123"})
    after = lead_profile(env, created.record_id)
    assert after["fields"]["email_status"] == before["fields"]["email_status"] == ["Geöffnet"]
    assert after["fields"]["appointment_reminder"] is True
    assert after["subforms"] == before["subforms"]
    env.service.execute("leads.update", {"lead_id": str(created.record_id), "lead_result": "Storniert"})
    assert lead_profile(env, created.record_id)["fields"]["appointment_reminder"] is False
    env.service.execute("leads.update", {"lead_id": str(created.record_id), "email_status": "[]", "phone": ""})
    assert lead_profile(env, created.record_id)["fields"]["email_status"] == []


def test_lead_html_unchecked_values_are_explicit_clears(env):
    created = env.service.execute("leads.create", {"company": "Example", "email_status": '["Geöffnet"]'})
    response = asyncio.run(web.update_lead_fields(created.record_id, request(env, {"lead_field__company": "Renamed"}), env.db))
    assert response.status_code == 303
    assert lead_profile(env, created.record_id)["fields"]["email_status"] == []
    assert lead_profile(env, created.record_id)["fields"]["company"] == "Renamed"


def test_customer_zoho_partial_update_keeps_omitted_checkbox_but_ui_can_clear_it(env):
    profile = {"fields": {"Kunde-Name": "Remote", "Bankverbindung zeigen": True}, "field_metadata": {
        "customer_name": {"label": "Kunde-Name", "api_name": "Account_Name", "editable": True},
        "show_bank_details": {"label": "Bankverbindung zeigen", "api_name": "Bankverbindung_zeigen", "display_type": "Boolesch", "editable": True}}, "subforms": {}}
    customer = Customer(name="Remote", zoho_id="remote-id", encrypted_profile_json=env.cipher.encrypt(json.dumps(profile)))
    env.db.add(customer)
    env.db.flush()
    changes = []

    def update(self, *, customer_id, submitted_values):
        changes.append(self._root_field_changes(profile["field_metadata"], profile, submitted_values))
        return customer

    env.monkeypatch.setattr(ZohoCrmService, "update_customer_fields", update)
    env.service.execute("customers.update", {"customer_id": str(customer.id), "customer_name": "Changed"})
    assert changes[-1] == {"Account_Name": "Changed"}
    asyncio.run(web.update_customer_fields(customer.id, request(env, {"customer_field__customer_name": "Changed"}), env.db))
    assert changes[-1] == {"Account_Name": "Changed", "Bankverbindung_zeigen": False}


@pytest.mark.parametrize("module", ["customers", "leads"])
def test_reads_updates_and_delete_respect_record_scope_and_inactive_user(env, module):
    fields = {"customer_name": "Hidden"} if module == "customers" else {"company": "Hidden"}
    record = env.service.execute(f"{module}.create", fields)
    identity = "customer_id" if module == "customers" else "lead_id"
    service = HubOperationService(db=env.db, cipher=env.cipher, actor="sales")
    for action in ("read", "update", *(["delete"] if module == "leads" else [])):
        with pytest.raises(HubOperationError):
            (service.query if action == "read" else service.execute)(f"{module}.{action}", {identity: str(record.record_id)})
    env.access.assign_created_record(user=env.sales, module_key=module, record_id=record.record_id)
    assert service.query(f"{module}.read", {identity: str(record.record_id)})[identity] == str(record.record_id)
    env.sales.is_active = False
    env.db.flush()
    with pytest.raises(HubOperationError):
        service.query(f"{module}.read", {identity: str(record.record_id)})


def test_search_filters_linked_contacts_cases_and_calendar_before_limiting(env):
    visible = env.service.execute("customers.create", {"customer_name": "Example visible"}).record_id
    hidden = env.service.execute("customers.create", {"customer_name": "Example hidden"}).record_id
    env.access.assign_created_record(user=env.sales, module_key="customers", record_id=visible)
    for customer_id in (visible, hidden):
        env.db.add(CustomerContact(customer_id=customer_id, encrypted_profile_json=env.cipher.encrypt(json.dumps({"fields": {"Name": f"Example contact {customer_id}"}}))))
        env.db.add(HubCase(customer_id=customer_id, case_number=f"Example-{customer_id}", encrypted_fields_json=env.cipher.encrypt('{}')))
        env.db.add(CustomerCallActivity(customer_id=customer_id, assignee_user_id=env.sales.id,
            name=f"Example call {customer_id}", starts_at=datetime(2026, 9, 19), ends_at=datetime(2026, 9, 19, 1)))
    env.db.flush()
    service = HubOperationService(db=env.db, cipher=env.cipher, actor="sales")
    groups = service.query("hub.search", {"query": "Example"})["groups"]
    items = {group["key"]: group["items"] for group in groups}
    assert all("hidden" not in json.dumps(group) for group in groups)
    for key in ("customers", "contacts", "cases", "calendar"):
        assert len(items[key]) == 1
    request_obj = request(env, {})
    request_obj.state = SimpleNamespace(hub_user=env.sales)
    response = web.global_search_suggestions(request_obj, env.db, q="Example")
    assert json.loads(response.body)["groups"] == groups
    assert HubGlobalSearchService(db=env.db, cipher=env.cipher).search("Example", include_admin_modules=True, visible_modules=set()) == []


def test_missing_customer_module_never_exposes_parent_contact_or_case(env):
    env.access.save_role(role_key="isolated", name="Isolated", description="Test", permissions={
        "contacts": {"view": True}, "cases": {"view": True}})
    env.sales.role = "isolated"
    customer = env.service.execute("customers.create", {"customer_name": "Hidden"}).record_id
    env.db.add(CustomerContact(customer_id=customer, encrypted_profile_json=env.cipher.encrypt('{"fields":{"Name":"Example"}}')))
    env.db.add(HubCase(customer_id=customer, case_number="Example", encrypted_fields_json=env.cipher.encrypt('{}')))
    env.db.flush()
    result = HubOperationService(db=env.db, cipher=env.cipher, actor="sales").query("hub.search", {"query": "Example"})
    assert result["groups"] == []


def call(name, values, call_id="read-1"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(values)}


def proposal(actions=None):
    return call("propose_hub_actions", {"summary": "Test", "response": "Test", "actions": actions or []})


def test_agent_reads_searches_and_only_proposes_mutation(env):
    created = env.service.execute("leads.create", {"company": "Example", "email": "lead@example.test"})
    before = env.db.get(HubLead, created.record_id).encrypted_profile_json
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    captured = []
    outputs = [
        {"output": [{"type": "reasoning", "id": "rs-1", "encrypted_content": "opaque", "summary": []}, call("hub_read", {"queries": [{"key": "hub.search", "input": {"query": "Example", "module": "leads"}}]})]},
        {"output": [call("hub_read", {"queries": [{"key": "leads.read", "input": {"lead_id": str(created.record_id)}}]}, "read-2")]},
        {"output": [proposal([{"action_type": "leads.update", "title": "Telefon", "details": "Test", "input": {"lead_id": str(created.record_id), "phone": "123"}}])]},
    ]

    def respond(*, api_key, payload):
        captured.append(copy.deepcopy(payload))
        return outputs.pop(0)

    env.monkeypatch.setattr(agent, "_create_openai_response", respond)
    result = agent._create_plan(api_key="test", model=DEFAULT_OPENAI_MODEL, actor="admin", instruction="Telefon aendern")
    assert result["actions"][0]["action_type"] == "leads.update"
    assert env.db.get(HubLead, created.record_id).encrypted_profile_json == before
    assert any(item.get("type") == "reasoning" for item in captured[1]["input"])
    tool_result = tool_output(captured[2])
    assert tool_result["untrusted_source_data"] is True
    assert "lead@example.test" in json.dumps(tool_result)
    assert all(payload["store"] is False for payload in captured)


@pytest.mark.parametrize("tool,values", [("leads.create", {"company": "Forbidden"}), ("leads__read", {"lead_id": "999"}), ("hub__search", {"query": "x" * 81}), ("hub__search", [])])
def test_planning_tools_fail_closed_without_writes(env, tool, values):
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    captured = []

    def respond(*, api_key, payload):
        captured.append(copy.deepcopy(payload))
        return {"output": [call(tool, values) if len(captured) == 1 else proposal()]}

    env.monkeypatch.setattr(agent, "_create_openai_response", respond)
    agent._create_plan(api_key="test", model=DEFAULT_OPENAI_MODEL, actor="admin", instruction="Test")
    assert "error" in tool_output(captured[1])
    assert env.db.scalars(select(HubLead)).all() == []


def test_planning_reads_have_a_strict_iteration_budget(env):
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    choices = []

    def respond(*, api_key, payload):
        choices.append(payload["tool_choice"])
        return {"output": [call("hub_read", {"queries": [{"key": "hub.search", "input": {"query": "Example"}}]})]}

    env.monkeypatch.setattr(agent, "_create_openai_response", respond)
    with pytest.raises(HubAgentError):
        agent._create_plan(api_key="test", model=DEFAULT_OPENAI_MODEL, actor="admin", instruction="Test")
    assert len(choices) == 9
    assert choices[-1] == {"type": "function", "name": "propose_hub_actions"}


def test_every_core_record_write_route_uses_shared_operations():
    tree = ast.parse(Path(web.__file__).read_text(encoding="utf-8"))
    routes = {node.name: ast.unparse(node) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, operation in {"create_customer_page": "customers.create", "update_customer_fields": "customers.update",
        "create_lead_page": "leads.create", "update_lead_fields": "leads.update", "delete_lead_from_hub": "leads.delete"}.items():
        assert operation in routes[name]
        assert "HubOperationService" in routes[name]
    assert {query.key for query in hub_queries()} >= {"hub.search", "customers.read", "leads.read"}


@pytest.mark.parametrize("module,identity", [("customers", "customer_id"), ("leads", "lead_id")])
def test_paginated_lists_only_count_visible_records(env, module, identity):
    visible_ids = []
    for index in range(28):
        values = {"customer_name" if module == "customers" else "company": f"Record {index:02}"}
        result = env.service.execute(f"{module}.create", values)
        if index < 27:
            visible_ids.append(str(result.record_id))
            env.access.assign_created_record(user=env.sales, module_key=module, record_id=result.record_id)
    service = HubOperationService(db=env.db, cipher=env.cipher, actor="sales")
    first = service.query(f"{module}.list", {})
    second = service.query(f"{module}.list", {"offset": first["next_offset"]})
    assert first["total"] == second["total"] == 27
    assert len(first["items"]) == 25
    assert second["next_offset"] is None
    assert {item[identity] for item in first["items"] + second["items"]} == set(visible_ids)


def test_read_text_pagination_and_stored_subform_indices(env):
    record = env.service.execute("leads.create", {"company": "Example", "opening_hours": "A" * 7000,
        "subform_values": json.dumps({"lead_subform__lead_results__new__lead_modified_at": "2026-01-01T12:00",
            "lead_subform__lead_results__new__lead_modified_by": "Old row"})})
    env.service.execute("leads.update", {"lead_id": str(record.record_id), "subform_values": json.dumps({
        "lead_subform__lead_results__new__lead_modified_at": "2026-09-01T12:00", "lead_subform__lead_results__new__lead_modified_by": "New row"})})
    data = env.service.query("leads.read", {"lead_id": str(record.record_id)})
    assert data["subforms"][0]["rows"][0]["fields"][0]["value"] == "Old row"
    assert data["subforms"][0]["rows"][0]["index"] == 0
    env.service.execute("leads.update", {"lead_id": str(record.record_id), "subform_values": json.dumps({
        "lead_subform__lead_results__0__lead_modified_by": "Changed old row"})})
    rows = lead_profile(env, record.record_id)["subforms"]["lead_results"]
    assert rows[0]["lead_modified_by"] == "Changed old row"
    assert rows[1]["lead_modified_by"] == "New row"
    data = env.service.query("leads.read", {"lead_id": str(record.record_id), "field": "opening_hours"})
    assert data["fields"][0]["next_text_offset"] == "6000"
    next_page = env.service.query("leads.read", {"lead_id": str(record.record_id), "field": "opening_hours", "text_offset": "6000"})
    assert next_page["fields"][0]["value"] == "A" * 1000
    assert next_page["fields"][0]["next_text_offset"] is None


def test_readonly_grant_cannot_find_sensitive_fields_or_write(env):
    env.access.save_role(role_key="limited", name="Limited", description="Test", permissions={
        "customers": {"view": True, "edit": True, "scope": "none"}})
    env.sales.role = "limited"
    profile = {"fields": {"Kunde-Name": "Example", "IBAN": "SECRET-BANK"}, "field_metadata": {
        "customer_name": {"label": "Kunde-Name", "editable": True},
        "iban": {"label": "IBAN", "sensitive": True, "editable": True}}}
    customer = Customer(name="Example", encrypted_profile_json=env.cipher.encrypt(json.dumps(profile)))
    env.db.add(customer)
    env.db.flush()
    env.access.add_grant(module_key="customers", record_id=customer.id, user_id=env.sales.id, team_id=None, can_edit=False)
    service = HubOperationService(db=env.db, cipher=env.cipher, actor="sales")
    assert "SECRET-BANK" not in json.dumps(service.query("customers.read", {"customer_id": str(customer.id)}))
    assert service.query("customers.list", {"query": "SECRET-BANK"})["total"] == 0
    with pytest.raises(HubOperationError):
        service.execute("customers.update", {"customer_id": str(customer.id), "customer_name": "Forbidden"})


def test_new_queries_are_discovered_without_changing_agent_dispatch(env):
    from app.services import hub_operations
    query = hub_operations.HubQuery("test.lookup", "Test", (), lambda service, values: {"value": "registered"})
    env.monkeypatch.setitem(hub_operations._QUERIES, query.key, query)
    agent = HubAgentService(db=env.db, cipher=env.cipher)
    captured = []

    def respond(*, api_key, payload):
        captured.append(copy.deepcopy(payload))
        return {"output": [call("hub_read", {"queries": [{"key": "test.lookup", "input": {}}]}) if len(captured) == 1 else proposal()]}

    env.monkeypatch.setattr(agent, "_create_openai_response", respond)
    agent._create_plan(api_key="test", model=DEFAULT_OPENAI_MODEL, actor="admin", instruction="Test")
    assert not any(tool["name"] == "test__lookup" for tool in captured[0]["tools"])
    assert tool_output(captured[1])["data"]["results"][0]["data"] == {"value": "registered"}


def test_unknown_subform_input_is_rejected_and_delete_stays_local(env):
    created = env.service.execute("leads.create", {"company": "Example"})
    with pytest.raises(HubOperationError):
        env.service.execute("leads.update", {"lead_id": str(created.record_id), "subform_values": '{"lead_subform__other__0__admin":"true"}'})
    response = asyncio.run(web.delete_lead_from_hub(created.record_id, request(env, {}), env.db))
    assert response.status_code == 303
    assert env.db.get(HubLead, created.record_id) is None


@pytest.mark.parametrize("active", [True, False])
def test_agent_approved_record_operation_rechecks_user_and_returns_chain_outputs(env, active):
    from app.models.hub_agent import HubAgentAction, HubAgentJob

    job = HubAgentJob(created_by_username="admin", status="ready",
        encrypted_request_json=env.cipher.encrypt('{"instruction":"Create"}'),
        encrypted_plan_json=env.cipher.encrypt('{"summary":"Create","response":"Test"}'))
    action = HubAgentAction(job=job, action_type="leads.create", sort_order=0, status="proposed",
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"title": "Create", "details": "Test", "input": {
            "company": "Agent example", "email": "lead@example.test"}})))
    env.db.add(action)
    env.db.flush()
    env.user.is_active = active
    env.db.flush()
    assert env.db.scalars(select(HubLead)).all() == []
    view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(actor="admin", action_id=action.id)
    if active:
        assert view.status == "completed"
        result = json.loads(env.cipher.decrypt(action.encrypted_result_json))
        assert result["outputs"]["recipient_email"] == "lead@example.test"
        assert result["outputs"]["lead_id"] == str(env.db.scalars(select(HubLead)).one().id)
    else:
        assert view.status == "failed"
        assert env.db.scalars(select(HubLead)).all() == []
