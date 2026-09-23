import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import html as html_parser
from starlette.datastructures import FormData
from starlette.requests import Request

from app.api.routes import accounts
from app.models.hub_user import HubUser
from app.services.hub_accounts import HubAccountService
from app.services.hub_agent import HubAgentService
from app.services.hub_operations import HubOperationError, get_operation
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from test_hub_document_template_operations import hub
from test_pdf_currency_display import snapshot_for


KINDS = ("offers", "orders", "invoices", "dunnings")


def create(hub, kind):
    return hub.execute("finance.pdf_templates.create", {"name": "Testvorlage", "document_type": kind}).outputs["template_id"]


def read(hub, template_id):
    return hub.query("finance.pdf_templates.read", {"template_id": template_id})


def render_settings(hub, template_id, kind):
    request = Request({"type": "http", "method": "GET", "scheme": "http", "server": ("hub.test", 80),
        "path": "/settings", "query_string": f"pdf_template_type={kind}&pdf_template={template_id}".encode(),
        "headers": [], "session": {}})
    user = hub.db.query(HubUser).filter_by(username="admin").one()
    service = HubAccountService(db=hub.db, app_secret_key="a" * 32)
    context = accounts._account_context(request, user, service, page_mode="settings")
    return accounts.templates.get_template("account.html").render(request=request, **context)


@pytest.mark.parametrize("kind", KINDS)
def test_default_and_toggle_match_preview_and_pdf(hub, kind):
    template_id = create(hub, kind)
    assert read(hub, template_id)["show_totals"] is False
    domain = HubPdfTemplateService(db=hub.db)
    template = domain.get(int(template_id))
    for enabled in (False, True, False):
        hub.execute("finance.pdf_templates.update_positions", {"template_id": template_id,
            "columns": json.dumps(read(hub, template_id)["columns"]), "show_totals": str(enabled).lower()})
        hub.db.flush()
        hub.db.expire_all()
        assert read(hub, template_id)["show_totals"] is enabled
        assert domain.editor_view(template).show_totals is enabled
        rendered = render_settings(hub, template_id, kind)
        dom = html_parser.fromstring(rendered)
        checkbox = dom.xpath('//input[@name="show_totals"]')[0]
        assert (checkbox.get("checked") is not None) is enabled
        assert bool(dom.xpath('//div[@class="pdf-template-preview-totals"]')) is enabled
        pdf_html = HubFinancePdfService(db=None, cipher=None)._render_html(snapshot=snapshot_for(kind),
            content=domain.decoded_content(document_type=kind, content_json=template.content_json))
        pdf_dom = html_parser.fromstring(pdf_html)
        assert bool(pdf_dom.xpath('//table[@class="totals"]')) is enabled
        assert "Website" in pdf_html and "180,00" in pdf_html
        if kind in ("invoices", "dunnings"):
            assert "214,20" in pdf_dom.xpath('//div[@class="payment"]')[0].text_content()
        if directory := os.environ.get("HUB_TEST_ARTIFACT_DIR"):
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{kind}-{'on' if enabled else 'off'}.html").write_text(rendered, encoding="utf-8")


def test_agent_uses_shared_operation_and_omission_preserves_choice(hub):
    template_id = create(hub, "offers")
    agent = object.__new__(HubAgentService)
    agent.db, agent.cipher = hub.db, None
    columns = json.dumps(read(hub, template_id)["columns"])
    agent._execute_payload(action_type="finance.pdf_templates.update_positions",
        payload={"input": {"template_id": template_id, "columns": columns, "show_totals": "true"}}, actor="admin")
    assert read(hub, template_id)["show_totals"] is True
    hub.execute("finance.pdf_templates.update_positions", {"template_id": template_id, "columns": columns})
    hub.execute("finance.pdf_templates.update_block", {"template_id": template_id, "block_key": "intro", "content_html": "<p>Test</p>"})
    copy_id = hub.execute("finance.pdf_templates.duplicate", {"template_id": template_id}).outputs["template_id"]
    assert read(hub, template_id)["show_totals"] is read(hub, copy_id)["show_totals"] is True
    assert not get_operation("finance.pdf_templates.update_positions").input_contract()["show_totals"]["required"]
    version = read(hub, template_id)["version"]
    for bad in ("yes", "", "0", "None"):
        with pytest.raises(HubOperationError):
            hub.execute("finance.pdf_templates.update_positions", {"template_id": template_id, "columns": columns, "show_totals": bad})
    assert read(hub, template_id)["version"] == version


@pytest.mark.parametrize("enabled", [True, False])
def test_http_checkbox_passes_explicit_selection_to_shared_operation(hub, monkeypatch, enabled):
    template_id = create(hub, "offers")
    columns = read(hub, template_id)["columns"]
    hub.execute("finance.pdf_templates.update_positions", {"template_id": template_id, "columns": json.dumps(columns), "show_totals": "true"})
    fields = [("csrf_token", "csrf")]
    for column in columns:
        for key in ("key", "label", "source_key", "width", "alignment"):
            fields.append(("column_" + ("source" if key == "source_key" else key), str(column[key])))
        if column["is_enabled"]:
            fields.append(("column_enabled", column["key"]))
    if enabled:
        fields.append(("show_totals", "true"))
    async def form():
        return FormData(fields)
    user = hub.db.query(HubUser).filter_by(username="admin").one()
    monkeypatch.setattr(accounts, "require_csrf", lambda *args: None)
    monkeypatch.setattr(accounts, "_require_admin_user", lambda request: user)
    monkeypatch.setattr(accounts, "_audit_pdf_template", lambda *args, **kwargs: None)
    response = asyncio.run(accounts.update_pdf_template_positions(int(template_id), SimpleNamespace(form=form), hub.db))
    assert response.status_code == 303
    assert read(hub, template_id)["show_totals"] is enabled


def test_migration_is_idempotent_and_keeps_old_revisions(hub):
    domain = HubPdfTemplateService(db=hub.db)
    previous = []
    for kind in KINDS:
        template = domain.get(int(create(hub, kind)))
        content = json.loads(template.content_json)
        content["positions"].pop("show_totals")
        template.content_json = json.dumps(content)
        domain._version(template=template, actor_username="test")
        revision = domain.revision_for(template)
        previous.append((template, revision, revision.content_json, template.version))
    explicit = create(hub, "offers")
    hub.execute("finance.pdf_templates.update_positions", {"template_id": explicit,
        "columns": json.dumps(read(hub, explicit)["columns"]), "show_totals": "true"})
    assert domain.initialize_totals_visibility() == 4
    assert domain.initialize_totals_visibility() == 0
    assert read(hub, explicit)["show_totals"] is True
    for template, revision, original, version in previous:
        assert template.version == version + 1
        assert not domain.editor_view(template).show_totals
        assert revision.content_json == original
        old = domain.decoded_content(document_type=template.document_type, content_json=original)
        assert old["positions"]["show_totals"] is True
        rendered = HubFinancePdfService(db=None, cipher=None)._render_html(snapshot=snapshot_for(template.document_type), content=old)
        assert '<table class="totals">' in rendered
        expected = json.loads(original)
        expected["positions"]["show_totals"] = False
        assert json.loads(template.content_json) == expected
