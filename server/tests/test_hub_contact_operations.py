import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from mailbox_fixture_helpers import mailbox_account

from app.api.routes import web
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_agent import HubAgentAction, HubAgentConversation, HubAgentJob
from app.models.hub_user import HubUser
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services import customer_directory, zoho_contact_field_catalog
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_agent import HubAgentError, HubAgentService
from app.services.hub_operations import HubOperationError, HubOperationService, agent_operations, get_operation
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
        db.add_all([user, customer, hidden])
        mailbox_account(db, cipher)
        db.commit()
        monkeypatch.setattr(web, "_require_hub_admin", lambda request: user)
        monkeypatch.setattr(web, "require_csrf", lambda request, token: None)
        monkeypatch.setattr(web, "get_csrf_token", lambda request: "test")
        monkeypatch.setattr(web, "write_audit_log", lambda *args, **kwargs: None)
        yield SimpleNamespace(db=db, cipher=cipher, user=user, customer=customer, hidden=hidden,
            service=HubOperationService(db=db, cipher=cipher, actor=user.username),
            directory=CustomerDirectoryService(db=db, cipher=cipher), monkeypatch=monkeypatch)
    engine.dispose()


def fields(**overrides):
    return {f"contact_field__{key}": value for key, value in {
        "salutation": "Frau", "first_name": "Lena", "last_name": "Test", "email": "lena@example.test",
        "secondary_email": "second@example.test", "phone": "0123456", "mailing_city": "Berlin", **overrides,
    }.items()}


def create(env, *, zoho=False, customer=None, **overrides):
    contact = env.directory.create_hub_contact(customer_id=(customer or env.customer).id, submitted_values=fields(**overrides))
    if zoho:
        contact.zoho_id = f"zoho-{contact.id}"
    env.db.commit()
    return contact


def request_for(env, values):
    class Request:
        state = SimpleNamespace(hub_user=env.user)

        async def form(self):
            return FormData(values)
    return Request()


def agent_action(env, operation, values, actor="admin"):
    job = HubAgentJob(created_by_username=actor, status="ready",
        encrypted_request_json=env.cipher.encrypt('{"instruction":"Test"}'),
        encrypted_plan_json=env.cipher.encrypt('{"summary":"Test","response":"Test"}'))
    action = HubAgentAction(job=job, action_type=operation, sort_order=0, status="proposed",
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"title": "Test", "details": "Test", "input": values})))
    env.db.add(action)
    env.db.flush()
    return action


def profile(env, contact):
    return json.loads(env.cipher.decrypt(contact.encrypted_profile_json))["fields"]


def mock_zoho(env):
    calls = []
    env.monkeypatch.setattr(ZohoCrmService, "_require_connected_connection", lambda self: object())

    def put(self, connection, path, body):
        calls.append(("PUT", path, body))
        return {"data": [{"status": "success", "details": {"id": body["data"][0]["id"]}}]}

    def get(self, connection, path, params):
        calls.append(("GET", path, params))
        return {"data": [{"id": path.rsplit("/", 1)[1], "Last_Name": "Synced", "Email": "synced@example.test"}]}

    env.monkeypatch.setattr(ZohoCrmService, "_api_put_json", put)
    env.monkeypatch.setattr(ZohoCrmService, "_api_get", get)
    return calls


@pytest.mark.parametrize("linked", [True, False])
def test_ui_and_agent_create_identical_contacts(env, linked):
    values = fields()
    if linked:
        values["customer_id"] = str(env.customer.id)
    response = asyncio.run(web.create_contact_page(request_for(env, values), env.db))
    assert response.status_code == 303
    human = env.db.scalars(select(CustomerContact)).one()
    action = agent_action(env, "contacts.create", values)
    view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor="admin")
    assert view.status == "completed", view.error
    agent = env.db.scalars(select(CustomerContact).where(CustomerContact.id != human.id)).one()
    assert profile(env, human) == profile(env, agent)
    assert human.customer_id == agent.customer_id == (env.customer.id if linked else None)
    assert human.zoho_id is agent.zoho_id is None
    result = json.loads(env.cipher.decrypt(action.encrypted_result_json))
    assert result["outputs"]["recipient_email"] == "lena@example.test"
    assert result["outputs"]["contact_id"] == str(agent.id)


@pytest.mark.parametrize("zoho", [False, True])
def test_ui_and_agent_update_use_same_service_and_preserve_omitted_fields(env, zoho):
    calls = mock_zoho(env)
    human, agent = create(env, zoho=zoho), create(env, zoho=zoho)
    changes = {"contact_field__phone": "98765", "contact_field__secondary_email": ""}
    if zoho:
        response = asyncio.run(web.update_customer_contact_fields(env.customer.id, human.id, request_for(env, changes), env.db))
    else:
        response = asyncio.run(web.update_hub_contact_fields(human.id, request_for(env, changes), env.db))
    assert "fields=success" in response.headers["location"]
    action = agent_action(env, "contacts.update", {"contact_id": str(agent.id), **changes})
    view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor="admin")
    assert view.status == "completed", view.error
    assert profile(env, human) == profile(env, agent)
    stored = profile(env, agent)
    assert stored["E-Mail"] == "lena@example.test"
    assert stored["Tel."] == "98765"
    assert not stored["Zweite E-Mail-Adresse"]
    if zoho:
        assert len(calls) == 2
        assert all(call[2]["data"][0]["Last_Name"] == "Test" for call in calls)
        assert all(call[2]["data"][0]["Email"] == "lena@example.test" for call in calls)
    else:
        assert calls == []


@pytest.mark.parametrize("action", ["link_customer", "delete", "sync"])
def test_ui_and_agent_other_contact_operations(env, action):
    calls = mock_zoho(env)
    human, agent = create(env, zoho=action == "sync"), create(env, zoho=action == "sync")
    values = {"contact_id": str(agent.id)}
    if action == "link_customer":
        values["new_customer_id"] = str(env.hidden.id)
        response = asyncio.run(web.update_hub_contact_link(human.id, request_for(env, {"customer_id": str(env.hidden.id)}), env.db))
    elif action == "delete":
        response = asyncio.run(web.delete_contact_from_hub(human.id, request_for(env, {}), env.db))
    else:
        response = web.sync_customer_contact(env.customer.id, human.id, request_for(env, {}), env.db)
    assert response.status_code == 303
    assert "error" not in response.headers["location"]
    proposed = agent_action(env, f"contacts.{action}", values)
    view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=proposed.id, actor="admin")
    assert view.status == "completed", view.error
    if action == "link_customer":
        assert human.customer_id == agent.customer_id == env.hidden.id
        env.service.execute("contacts.link_customer", {"contact_id": str(agent.id), "new_customer_id": ""})
        assert agent.customer_id is None
    elif action == "delete":
        assert env.db.scalars(select(CustomerContact)).all() == []
        assert calls == []
    else:
        assert len(calls) == 2
        assert profile(env, human) == profile(env, agent)
        assert profile(env, agent)["E-Mail"] == "synced@example.test"


@pytest.mark.parametrize("action", ["create", "update", "link_customer", "delete", "sync"])
def test_contact_operations_enforce_permissions_and_parent_scope(env, action):
    calls = mock_zoho(env)
    contact = create(env, zoho=action == "sync", customer=env.hidden)
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    sales = HubUser(username="sales", password_hash="x", role="sales")
    viewer = HubUser(username="viewer", password_hash="x", role="accounting")
    inactive = HubUser(username="inactive", password_hash="x", role="admin", is_active=False)
    env.db.add_all([sales, viewer, inactive])
    env.db.flush()
    access.assign_created_record(user=sales, module_key="customers", record_id=env.customer.id)
    values = {"contact_id": str(contact.id), "new_customer_id": str(env.customer.id), **fields()}
    if action == "create":
        values["customer_id"] = str(env.hidden.id)
    for user in (sales, viewer, inactive):
        service = HubOperationService(db=env.db, cipher=env.cipher, actor=user.username)
        with pytest.raises(HubOperationError):
            service.execute(f"contacts.{action}", values)
    assert calls == []
    assert env.db.scalars(select(CustomerContact)).all() == [contact]


def test_relink_requires_explicit_target_and_access_to_old_and_new_customer(env):
    contact = create(env)
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    sales = HubUser(username="sales", password_hash="x", role="sales")
    env.db.add(sales)
    env.db.flush()
    access.assign_created_record(user=sales, module_key="customers", record_id=env.customer.id)
    service = HubOperationService(db=env.db, cipher=env.cipher, actor=sales.username)
    for target in ({}, {"new_customer_id": "invalid"}, {"new_customer_id": str(env.hidden.id)}):
        with pytest.raises(HubOperationError):
            service.execute("contacts.link_customer", {"contact_id": str(contact.id), **target})
        assert contact.customer_id == env.customer.id
    service.execute("contacts.update", {"contact_id": str(contact.id), "contact_field__phone": "123"})
    assert profile(env, contact)["Tel."] == "123"


def test_zoho_link_is_immutable_and_delete_is_local_only(env):
    calls = mock_zoho(env)
    contact = create(env, zoho=True)
    with pytest.raises(ValueError):
        env.service.execute("contacts.link_customer", {"contact_id": str(contact.id), "new_customer_id": ""})
    env.service.execute("contacts.delete", {"contact_id": str(contact.id)})
    assert calls == []


def test_sync_rejects_local_contact_and_disconnected_zoho(env):
    contact = create(env)
    with pytest.raises(HubOperationError):
        env.service.execute("contacts.sync", {"contact_id": str(contact.id)})
    contact.zoho_id = "zoho-1"
    def fail(self):
        raise ZohoCrmError("Not connected")
    env.monkeypatch.setattr(ZohoCrmService, "_require_connected_connection", fail)
    action = agent_action(env, "contacts.sync", {"contact_id": str(contact.id)})
    view = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor="admin")
    assert view.status == "failed"
    assert view.error == "Not connected"


def test_name_resolution_is_unique_and_customer_scope_must_match(env):
    contact = create(env)
    result = env.service.execute("contacts.update", {"target_name": "Lena Test", "contact_field__phone": "555"})
    assert result.record_id == contact.id
    with pytest.raises(HubOperationError):
        env.service.execute("contacts.delete", {"contact_id": str(contact.id), "customer_id": str(env.hidden.id)})
    create(env)
    with pytest.raises(HubOperationError):
        env.service.execute("contacts.delete", {"target_name": "Lena Test"})


def test_catalog_drives_create_form_validation_and_agent_without_prompt_rules(env):
    definitions = zoho_contact_field_catalog.contact_fields(creating=True)
    contract = get_operation("contacts.create").input_contract()
    macro = web.templates.env.get_template("partials/contact_field_control.html").module.contact_field_control
    for field in definitions:
        name = f"contact_field__{field.key}"
        assert contract[name]["required"] == field.required
        assert (" required" in macro(field, "")) == field.required
        if field.options:
            assert contract[name]["options"] == dict(field.options)
    assert contract["contact_field__salutation"]["required"]
    assert contract["contact_field__last_name"]["required"]
    for missing in ("salutation", "last_name"):
        with pytest.raises(ValueError):
            env.service.execute("contacts.create", fields(**{missing: ""}))
    assert "Ein Kontakt braucht mindestens" not in Path("app/services/hub_agent.py").read_text(encoding="utf-8")
    assert "create_contact" not in {operation.key for operation in agent_operations()}
    assert get_operation("create_contact") is None


def test_added_catalog_field_is_automatically_available_to_ui_and_agent(env):
    catalog = zoho_contact_field_catalog.ZOHO_CONTACT_FIELDS + (
        zoho_contact_field_catalog.ZohoContactField("test_extra", "Extra field", "Extra_Field", "Einzelzeile"),
    )
    env.monkeypatch.setattr(zoho_contact_field_catalog, "ZOHO_CONTACT_FIELDS", catalog)
    env.monkeypatch.setattr(customer_directory, "ZOHO_CONTACT_FIELDS", catalog)
    contract = get_operation("contacts.create").input_contract()
    assert contract["contact_field__test_extra"]["required"] is False
    result = env.service.execute("contacts.create", fields(test_extra="Extra content"))
    detail = env.directory.get_contact_detail_by_id(contact_id=result.record_id)
    assert next(field for field in detail.editable_profile_fields if field.key == "test_extra").form_value == "Extra content"
    context = web._contact_create_context(request_for(env, {}), env.db)
    assert any(field.key == "test_extra" for field in context["fields"])


def test_contact_context_is_fresh_and_rechecks_access(env):
    contact = create(env)
    service = HubAgentService(db=env.db, cipher=env.cipher)
    chat = service.start_conversation(actor="admin")
    service.add_context(actor="admin", conversation_id=chat.conversation_id, resource_type="contact", resource_key=str(contact.id))
    conversation = env.db.get(HubAgentConversation, chat.conversation_id)
    env.service.execute("contacts.update", {"contact_id": str(contact.id), "contact_field__phone": "987654"})
    prompt = service._conversation_prompt_contexts(conversation, exclude_email_key="")[0]
    assert "987654" in prompt and f"Kontakt-ID: {contact.id}" in prompt
    env.user.is_active = False
    assert service._conversation_prompt_contexts(conversation, exclude_email_key="") == ()
    with pytest.raises(HubAgentError):
        service.add_context(actor="admin", conversation_id=chat.conversation_id, resource_type="contact", resource_key=str(contact.id))


def test_detail_and_context_hide_contacts_from_inaccessible_customer(env):
    contact = create(env, customer=env.hidden)
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    sales = HubUser(username="sales", password_hash="x", role="sales")
    env.db.add(sales)
    env.db.flush()
    request = request_for(env, {})
    request.state = SimpleNamespace(hub_user=sales)
    with pytest.raises(HTTPException) as caught:
        web._contact_detail_context(request, env.db, detail=env.directory.get_contact_detail_by_id(contact_id=contact.id),
            fields="", fields_message="", layout="", layout_message="")
    assert caught.value.status_code == 404
    service = HubAgentService(db=env.db, cipher=env.cipher)
    chat = service.start_conversation(actor="sales")
    with pytest.raises(HubAgentError):
        service.add_context(actor="sales", conversation_id=chat.conversation_id, resource_type="contact", resource_key=str(contact.id))


def test_all_six_contact_record_write_routes_use_shared_gateway():
    names = {"create_contact_page", "update_hub_contact_fields", "update_hub_contact_link", "delete_contact_from_hub",
             "update_customer_contact_fields", "sync_customer_contact"}
    tree = ast.parse(Path("app/api/routes/web.py").read_text(encoding="utf-8"))
    checked = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            calls = [child for child in ast.walk(node) if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)]
            assert any(call.func.id == "_execute_contact_operation" for call in calls)
            assert not any(call.func.id in {"CustomerDirectoryService", "ZohoCrmService"} for call in calls)
            checked.add(node.name)
    assert checked == names


def test_contact_results_can_feed_an_unsent_email_draft(env):
    service = HubAgentService(db=env.db, cipher=env.cipher)
    first = agent_action(env, "contacts.create", {**fields(), "customer_id": str(env.customer.id)})
    second = HubAgentAction(job=first.job, action_type="emails.drafts.create", sort_order=1, status="proposed",
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"title": "Draft", "details": "Unsent", "input": {
            "recipient_email": "{{action.1.recipient_email}}", "recipient_name": "{{action.1.recipient_name}}",
            "customer_id": "{{action.1.customer_id}}", "subject": "Welcome", "content": "<p>Welcome</p>",
        }})))
    env.db.add(second)
    env.db.flush()
    assert env.db.scalars(select(CustomerContact)).all() == []
    assert env.db.scalars(select(HubMailboxEmail)).all() == []
    assert service.execute_action(action_id=first.id, actor="admin").status == "completed"
    assert env.db.scalars(select(HubMailboxEmail)).all() == []
    view = service.execute_action(action_id=second.id, actor="admin")
    assert view.status == "completed", view.error
    draft = env.db.scalars(select(HubMailboxEmail)).one()
    assert draft.mailbox_state == "draft"
    assert "lena@example.test" in env.cipher.decrypt(draft.encrypted_payload_json)


@pytest.mark.parametrize("role", ["admin", "sales", "accounting"])
def test_contact_editor_actions_match_the_users_permissions(env, role):
    from jinja2 import ChoiceLoader, DictLoader
    contact = create(env, zoho=True)
    access = HubAccessControlService(db=env.db)
    access.ensure_defaults()
    user = HubUser(username=f"test-{role}", password_hash="x", role=role)
    env.db.add(user)
    env.db.flush()
    access.assign_created_record(user=user, module_key="customers", record_id=env.customer.id)
    request = request_for(env, {})
    request.state = SimpleNamespace(hub_user=user)
    context = web._contact_detail_context(request, env.db, detail=env.directory.get_contact_detail_by_id(contact_id=contact.id),
        fields="", fields_message="", layout="", layout_message="")
    template_env = web.templates.env.overlay(loader=ChoiceLoader([
        DictLoader({"base.html": "{% block content %}{% endblock %}"}), web.templates.env.loader,
    ]))
    html = template_env.get_template("customer_contact_detail.html").render(**context)
    assert ("data-customer-edit-open" in html) == (role in {"admin", "sales"})
    assert ("data-contact-delete-open" in html) == (role == "admin")
    assert ("Aus Zoho synchronisieren" in html) == (role == "admin")


def test_invalid_customer_id_never_creates_an_unlinked_contact(env):
    env.monkeypatch.setattr(web.templates, "TemplateResponse", lambda request, name, context, status_code=200:
        SimpleNamespace(status_code=status_code, context=context))
    response = asyncio.run(web.create_contact_page(request_for(env, {**fields(), "customer_id": "invalid"}), env.db))
    assert response.status_code == 400
    assert response.context["error"]
    assert env.db.scalars(select(CustomerContact)).all() == []
