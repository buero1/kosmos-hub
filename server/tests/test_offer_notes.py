import asyncio
import json
import os
from pathlib import Path
from dataclasses import replace

import pytest
from fastapi import BackgroundTasks
from lxml import html as html_parser
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.models.hub_finance_offer import HubFinanceOffer
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_offer_notes import OFFER_NOTES_TOKEN, sanitize_offer_notes
from app.services.hub_operations import get_operation
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.template_placeholders import pdf_placeholders
from test_hub_finance_operations import env, read, req, values
from test_pdf_currency_display import snapshot_for


def domain(env):
    return HubFinanceService(db=env.db, cipher=env.cipher)


def test_migration_keeps_intro_and_moves_payment_to_notes_without_changing_history(env):
    templates = HubPdfTemplateService(db=env.db)
    template = templates.default_for("offers")
    intro = "<p>Hallo ${contactName},</p><p>Unser Angebot.</p>"
    original = "<p><strong>Anmerkungen</strong></p><p>Mindestlaufzeit: 24 Monate. Gueltig bis ${validUntil}.</p>"
    content = json.loads(template.content_json)
    content.pop("offer_notes_default_html")
    content.pop("offer_notes_block")
    content["blocks"]["intro"]["content_html"] = intro
    content["blocks"]["payment"]["content_html"] = original
    template.content_json = json.dumps(content)
    templates._version(template=template, actor_username="test")
    revision = templates.revision_for(template)
    previous = revision.content_json
    version = template.version
    assert templates.offer_notes_default() == original
    assert templates.enable_default_offer_notes()
    assert template.version == version + 1
    assert revision.content_json == previous
    assert not templates.enable_default_offer_notes()
    content = templates.decoded_content(document_type="offers", content_json=template.content_json)
    assert content["blocks"]["intro"]["content_html"] == intro
    assert content["blocks"]["payment"]["content_html"] == OFFER_NOTES_TOKEN
    assert content["offer_notes_block"] == "payment"
    assert templates.offer_notes_default() == original
    templates.update_block(actor=env.user, template_id=template.id, block_key="footer", content_html="<p>Footer</p>", is_visible=True)
    assert templates.offer_notes_default() == original


@pytest.mark.parametrize("changed_intro", [False, True])
def test_correct_initial_binding_restores_greeting_or_keeps_later_custom_intro(env, changed_intro):
    templates = HubPdfTemplateService(db=env.db)
    template = templates.default_for("offers")
    content = json.loads(template.content_json)
    content.pop("offer_notes_block")
    intro = "<p>Sehr geehrte/r ${contactName},</p><p>Vielen Dank.</p>"
    remarks = "<p>Anmerkungen</p><p>Netto zzgl. 19% USt. Mindestlaufzeit: 24 Monate.</p>"
    content["offer_notes_default_html"] = intro
    content["blocks"]["intro"]["content_html"] = "<p>Neue persoenliche Ansprache</p>" if changed_intro else OFFER_NOTES_TOKEN
    content["blocks"]["payment"]["content_html"] = remarks
    template.content_json = json.dumps(content)
    assert templates.offer_notes_default() == remarks
    assert templates.enable_default_offer_notes()
    corrected = json.loads(template.content_json)
    assert corrected["blocks"]["intro"]["content_html"] == ("<p>Neue persoenliche Ansprache</p>" if changed_intro else intro)
    assert corrected["blocks"]["payment"]["content_html"] == OFFER_NOTES_TOKEN
    assert templates.offer_notes_default() == remarks
    assert not templates.enable_default_offer_notes()
    blocks = {block.key: block for block in templates.editor_view(template).blocks}
    assert blocks["payment"].content_html == OFFER_NOTES_TOKEN
    assert "Mindestlaufzeit: 24 Monate" in blocks["payment"].preview_html
    assert "Mindestlaufzeit" not in blocks["intro"].preview_html


@pytest.mark.parametrize("notes", ["<p>Individuelle Konditionen bis ${Offer.ValidUntil}.</p>", ""])
def test_pdf_notes_do_not_replace_greeting_and_appear_after_totals(notes):
    service = HubFinancePdfService(db=None, cipher=None)
    snapshot = replace(snapshot_for("offers"), notes_html=notes, contact_name="Max Muster")
    content = HubPdfTemplateService._default_content("offers")
    content["positions"]["show_totals"] = True
    rendered = service._render_html(snapshot=snapshot, content=content)
    assert "Guten Tag Max Muster" in rendered and "vielen Dank" in rendered
    assert rendered.index("Guten Tag Max Muster") < rendered.index("Website")
    if notes:
        assert rendered.index("214,20") < rendered.index("Individuelle Konditionen bis 20.10.2026")
    assert '${' not in rendered


@pytest.mark.parametrize("source", ["ui", "agent"])
def test_shared_defaults_save_and_read(env, source):
    templates = HubPdfTemplateService(db=env.db)
    template = templates.default_for("offers")
    content = json.loads(template.content_json)
    content["offer_notes_default_html"] = "<p>Unser Standard.</p>"
    template.content_json = json.dumps(content)
    if source == "agent":
        record_id = env.service.execute("finance.offers.create", values(env, "offers")).record_id
    else:
        response = asyncio.run(web.create_finance_offer_page(req(env, values(env, "offers")), BackgroundTasks(), env.db))
        record_id = int(response.headers["location"].split("/")[-1].split("?")[0])
    record = env.db.get(HubFinanceOffer, record_id)
    assert "Unser Standard" not in record.encrypted_fields_json
    assert domain(env).get_offer_detail(offer_id=record_id).notes_html == "<p>Unser Standard.</p>"
    assert read(env, "offers", record_id)["input_values"]["offer_field__notes"] == "<p>Unser Standard.</p>"
    env.service.execute("finance.offers.update", {"record_id": str(record_id), "offer_field__reference": "Other edit"})
    assert domain(env).get_offer_detail(offer_id=record_id).notes_html == "<p>Unser Standard.</p>"


@pytest.mark.parametrize("notes", ["<p><b>Individuell</b> ${Contact.Name}</p>", "", "<p><br></p>"])
def test_edit_duplicate_pdf_and_explicit_empty(env, notes):
    record_id = env.service.execute("finance.offers.create", values(env, "offers")).record_id
    env.service.execute("finance.offers.update", {"record_id": str(record_id), "offer_field__notes": notes})
    expected = sanitize_offer_notes(notes)
    assert domain(env).get_offer_detail(offer_id=record_id).notes_html == expected
    copy = env.service.execute("finance.offers.duplicate", {"record_id": str(record_id)})
    assert domain(env).get_offer_detail(offer_id=copy.record_id).notes_html == expected
    snapshot = HubFinancePdfService(db=env.db, cipher=env.cipher)._snapshot(document_type="offers", document_id=record_id)
    assert snapshot.notes_html == expected


def test_legacy_offer_gets_default_without_read_writes_and_duplicate_keeps_it(env):
    record_id = env.service.execute("finance.offers.create", values(env, "offers")).record_id
    record = env.db.get(HubFinanceOffer, record_id)
    data = domain(env)._values(record.encrypted_fields_json)
    data.pop("notes")
    record.encrypted_fields_json = domain(env)._encrypt(data)
    env.db.commit()
    original = record.encrypted_fields_json
    expected = HubPdfTemplateService(db=env.db).offer_notes_default()
    assert domain(env).get_offer_detail(offer_id=record_id).notes_html == expected
    assert read(env, "offers", record_id)["input_values"]["offer_field__notes"] == expected
    assert record.encrypted_fields_json == original and not env.db.dirty
    copy = env.service.execute("finance.offers.duplicate", {"record_id": str(record_id)})
    assert domain(env).get_offer_detail(offer_id=copy.record_id).notes_html == expected


def test_contract_and_pdf_picker_are_offers_only():
    for action in ("create", "update"):
        field = get_operation(f"finance.offers.{action}").input_contract()["offer_field__notes"]
        assert not field["required"] and field["encoding"] == "HTML"
    assert OFFER_NOTES_TOKEN in {field.token for field in pdf_placeholders("offers")}
    for kind in ("orders", "invoices", "dunnings"):
        assert OFFER_NOTES_TOKEN not in {field.token for field in pdf_placeholders(kind)}
        assert "offer_notes_default_html" not in HubPdfTemplateService._default_content(kind)
        assert "document_field__notes" not in get_operation(f"finance.{kind}.update").input_contract()


def test_notes_html_is_safe_and_placeholders_resolved_once():
    notes = '<p onclick="bad()">Hallo ${Contact.Name}</p><p><strong>Wichtig</strong></p><script>bad()</script><a href="javascript:bad()">Link</a>'
    snapshot = replace(snapshot_for("offers"), notes_html=notes, contact_name='<img src=x onerror=bad()>')
    html = HubFinancePdfService(db=None, cipher=None)._render_html(snapshot=snapshot, content=HubPdfTemplateService._default_content("offers"))
    assert '<strong>Wichtig</strong>' in html
    assert '&lt;img src=x onerror=bad()&gt;' in html
    dom = html_parser.fromstring(html)
    assert not dom.xpath('//script | //*[@onclick or @onerror] | //a[starts-with(@href,"javascript:")]')
    assert '${Contact.Name}' not in html and OFFER_NOTES_TOKEN not in html
    assert html.index('Website') < html.index('<strong>Wichtig</strong>')


def test_bad_notes_cannot_overwrite_saved_record(env):
    record_id = env.service.execute("finance.offers.create", values(env, "offers")).record_id
    original = domain(env).get_offer_detail(offer_id=record_id).notes_html
    with pytest.raises(ValueError, match="lang"):
        env.service.execute("finance.offers.update", {"record_id": str(record_id), "offer_field__notes": "x" * 20001})
    assert domain(env).get_offer_detail(offer_id=record_id).notes_html == original


def test_render_notes_panel_and_export_browser_fixtures(env):
    data = values(env, "offers")
    data["offer_field__notes"] = "<p>Original <strong>Anmerkung</strong>.</p>"
    record_id = env.service.execute("finance.offers.create", data).record_id
    app = create_app()
    request = Request({"type": "http", "method": "GET", "path": f"/finance/offers/{record_id}", "query_string": b"",
                       "headers": [], "scheme": "http", "server": ("hub.test", 80), "session": {}, "app": app, "router": app.router})
    request.state.hub_user = env.user
    original = web.finance_offer_detail_page(record_id, request, env.db).body.decode()
    dom = html_parser.fromstring(original)
    panel = dom.get_element_by_id("finance-offer-notes")
    assert panel.xpath('./details')[0].get('open') is None
    assert 'finance-positions-panel' in panel.xpath('following-sibling::article[1]')[0].get('class')
    assert len(dom.xpath('//textarea[@name="offer_field__notes"]')) == 1
    assert panel.xpath('.//textarea[@name="offer_field__notes"]')[0].get('form') == 'finance-offer-fields-form'
    assert not dom.get_element_by_id('finance-offer-fields').xpath('.//*[text()="Anmerkungen"]')
    env.service.execute("finance.offers.update", {"record_id": str(record_id), "offer_field__notes": "<p>Gespeicherte <strong>Anmerkung</strong>.</p>"})
    saved = web.finance_offer_detail_page(record_id, request, env.db).body.decode()
    context = web._finance_offer_create_context(request, env.db)
    created = web.templates.TemplateResponse(request, "finance_offer_create.html", context).body.decode()
    assert context["submitted_values"]["offer_field__notes"] == HubPdfTemplateService(db=env.db).offer_notes_default()
    created_dom = html_parser.fromstring(created)
    notes_panel = created_dom.get_element_by_id("finance-offer-notes")
    assert notes_panel.xpath('./details')[0].get('open') is None
    assert len(created_dom.xpath('//textarea[@name="offer_field__notes"]')) == 1
    notes_input = notes_panel.xpath('.//textarea[@name="offer_field__notes"]')[0]
    assert notes_input.get('form') == 'finance-offer-create-form'
    assert notes_input.text == context["submitted_values"]["offer_field__notes"]
    assert not notes_panel.xpath('preceding-sibling::article[1]//textarea[@name="offer_field__notes"]')
    assert 'finance-positions-panel' in notes_panel.xpath('following-sibling::article[1]')[0].get('class')
    if directory := os.environ.get("HUB_TEST_ARTIFACT_DIR"):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        for name, content in (("original", original), ("saved", saved), ("create", created)):
            (path / f"{name}.html").write_text(content, encoding="utf-8")
        (path / "data.json").write_text(json.dumps({"record_id": record_id}), encoding="utf-8")
