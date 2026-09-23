import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.api.routes import web
from app.models.hub_lead import HubLead
from app.models.site import Site
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_operations import HubOperationError
from app.services.hub_template_contexts import context_from_path, record_values
from app.services.template_placeholders import email_document_placeholders, RECURRING_INVOICE_PLACEHOLDERS
from test_hub_email_composition import template, contact, draft_payload
from test_hub_mailbox_operations import env, mailbox
from test_hub_crm_reads import case, activity
from test_hub_finance_operations import values as finance_values


def lead(env):
    row = HubLead(encrypted_profile_json=env.cipher.encrypt(json.dumps({"fields": {
        "company": "Website prospect", "first_name": "Ada", "last_name": "Example", "email": "ada@example.test"}})))
    env.db.add(row)
    env.db.flush()
    HubAccessControlService(db=env.db).assign_record(module_key="leads", record_id=row.id, owner_user_id=env.sales.id, team_id=None)
    env.db.commit()
    return row


def test_lead_context_same_in_ui_agent_and_saved_draft(env):
    row = lead(env)
    template(env, subject="${Lead.Company}", content="<p>${Lead.FirstName} ${Lead.Email}</p>", hub_context_module="leads")
    inputs = {"template_id": "source", "context_module": "leads", "context_record_id": str(row.id)}
    rendered = env.limited.query("emails.templates.render", inputs)
    ui = web.mailbox_compose_template_preview("source", None, env.db, context_path=f"/leads/{row.id}")
    assert ui["content"] == rendered["content"] and "Ada" in ui["content"]
    assert ui["subject"] == "Website prospect" and not ui["unresolved_placeholders"]
    result = env.service.execute("emails.drafts.from_template", {**inputs, "recipient_email": "ada@example.test"})
    data = draft_payload(env, result)
    assert data["recipient_lead_id"] == row.id and data["context_module"] == "leads" and data["context_record_id"] == str(row.id)
    env.service.execute("emails.drafts.update", {"draft_id": str(result.record_id), "subject": "Adjusted"})
    reopened = mailbox(env).get_draft_compose_context(draft_id=result.record_id)
    assert reopened["subject"] == "Adjusted" and reopened["context_record_id"] == str(row.id)


@pytest.mark.parametrize("kind", ["offers", "orders", "invoices", "dunnings", "recurring-invoices"])
def test_finance_context_uses_real_fields_totals_and_customer(env, kind):
    person = contact(env, env.own)
    finance_env = SimpleNamespace(service=env.service, customer=env.own, contact=person)
    created = env.service.execute(f"finance.{kind}.create", finance_values(finance_env, kind))
    values, customer, lead_id = record_values(env.service, kind, created.record_id)
    assert customer == env.own.id and lead_id is None
    definitions = RECURRING_INVOICE_PLACEHOLDERS if kind == "recurring-invoices" else email_document_placeholders(kind)
    assert all(item.token[2:-1] in values for item in definitions if item.profile_key)
    namespace = {"offers": "Offer", "orders": "Order", "invoices": "Invoice", "dunnings": "Dunning", "recurring-invoices": "RecurringInvoice"}[kind]
    token = f"{namespace}.Name" if kind == "recurring-invoices" else f"{namespace}.GrossTotal"
    assert values[token]
    template(env, subject="Test", content="<p>${" + token + "}</p>")
    rendered = env.service.query("emails.templates.render", {"template_id": "source", "context_module": kind, "context_record_id": str(created.record_id)})
    assert not rendered["unresolved_placeholders"] and rendered["template_context"]["customer_id"] == str(env.own.id)
    with pytest.raises(HubOperationError, match="gehoert"):
        env.service.query("emails.templates.render", {"template_id": "source", "context_module": kind,
            "context_record_id": str(created.record_id), "customer_id": str(env.hidden.id)})


@pytest.mark.parametrize("kind", ["task", "call", "meeting"])
def test_activity_contexts_use_local_time_without_changing_status(env, kind):
    row = activity(env, env.own, kind, description="Read-only context")
    namespace = {"task": "Task", "call": "Call", "meeting": "Meeting"}[kind]
    field = "DueAt" if kind == "task" else "StartsAt"
    template(env, content=f"<p>${{{namespace}.Title}} ${{{namespace}.{field}}} ${{{namespace}.Status}}</p>")
    rendered = env.limited.query("emails.templates.render", {"template_id": "source", "context_module": kind + "s", "context_record_id": str(row.id)})
    assert "01.08.2026 11:00" in rendered["content"] and "Geplant" in rendered["content"]
    assert not rendered["unresolved_placeholders"] and row.status == "planned" and not env.db.dirty and not env.db.new


def test_case_and_site_contexts_are_scoped_and_do_not_expose_arbitrary_attributes(env):
    own, hidden = case(env, env.own), case(env, env.hidden)
    template(env, content="<p>${Case.Description}</p>")
    rendered = env.limited.query("emails.templates.render", {"template_id": "source", "context_module": "cases", "context_record_id": str(own.id)})
    assert "Example case" in rendered["content"]
    with pytest.raises(HubOperationError):
        env.limited.query("emails.templates.render", {"template_id": "source", "context_module": "cases", "context_record_id": str(hidden.id)})
    site = Site(uuid="example", domain="example.test", home_url="https://example.test", site_url="https://example.test", customer_id=env.own.id)
    secret = Site(uuid="secret", domain="secret.test", home_url="https://secret.test", site_url="https://secret.test", customer_id=env.hidden.id)
    env.db.add_all([site, secret])
    env.db.commit()
    values, customer, _ = record_values(env.service, "sites", site.id)
    assert values["Site.Domain"] == "example.test" and customer == env.own.id
    assert not any("connection" in key.lower() or "password" in key.lower() for key in values)


@pytest.mark.parametrize("module, record_id", [("leads", ""), ("", "1"), ("unknown", "1"), ("general", "1"), ("cases", "-1")])
def test_invalid_context_never_falls_back_to_some_other_record(env, module, record_id):
    template(env)
    with pytest.raises(HubOperationError):
        env.service.query("emails.templates.render", {"template_id": "source", "context_module": module, "context_record_id": record_id})


def test_general_context_and_unknown_values(env):
    template(env, content="<p>${Company.Name} ${Lead.Company}</p>")
    rendered = env.service.query("emails.templates.render", {"template_id": "source"})
    assert "${Company.Name}" not in rendered["content"] and "${Lead.Company}" in rendered["content"]
    assert rendered["unresolved_placeholders"]
    assert context_from_path("/finance/offers/10") == ("offers", "10")
    assert context_from_path("/activities/task/3") == ("tasks", "3")
    assert context_from_path("https://external.test/leads/1") == ("", "")


def test_both_email_editors_send_and_preserve_context_without_stale_responses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the actual composer JavaScript")
    for path in ("app/templates/emails.html", "app/templates/partials/global_mailbox_composer.html"):
        source = Path(path).read_text(encoding="utf-8")
        assert 'name="context_module"' in source and 'name="context_record_id"' in source
    completed = subprocess.run([node, "tests/js/email_template_context.cjs"], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_offer_page_takes_precedence_over_its_linked_lead(env):
    row = lead(env)
    offer = env.service.execute("finance.offers.create", {"lead_id": str(row.id),
        "offer_line__0__name": "Website", "offer_line__0__unit_price": "100"})
    template(env, content="<p>${Offer.Number}: ${Lead.Company}</p>")
    rendered = web.mailbox_compose_template_preview("source", None, env.db, lead_id=str(row.id), context_path=f"/finance/offers/{offer.record_id}")
    assert rendered["template_context"]["context_module"] == "offers"
    assert "Website prospect" in rendered["content"] and not rendered["unresolved_placeholders"]


def test_untrusted_lead_values_are_escaped_in_template_preview(env):
    row = lead(env)
    row.encrypted_profile_json = env.cipher.encrypt(json.dumps({"fields": {"company": '<img src=x onerror="alert(1)">', "website": "javascript:alert(1)"}}))
    env.db.commit()
    template(env, content='<p>${Lead.Company}</p><a href="${Lead.Website}">Site</a>')
    rendered = env.service.query("emails.templates.render", {"template_id": "source", "lead_id": str(row.id)})
    assert '<img src=x' not in rendered["content"] and 'href="javascript:' not in rendered["content"]
