import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import web
from app.models.customer import Customer
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.models.module_layout import ModuleLayout
from app.services import module_layout_catalog, hub_operation_pdf_reads
from app.services.hub_finance_documents import HubFinanceDocumentService
from app.services.hub_finance_operations_shared import PDF_KINDS
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_finance_pdf_readers import load_pdf
from app.services.hub_operations import HubOperationError, HubOperationPending, HubOperationService
from app.services.module_layout_catalog import module_layout_definitions
from test_hub_finance_operations import env, req, values
from test_hub_mailbox_operations import env as crm_env
from test_hub_crm_reads import case


LAYOUT_KEYS = tuple(item.layout_key for item in module_layout_definitions())


@pytest.mark.parametrize("layout_key", LAYOUT_KEYS)
def test_layout_catalog_ui_and_save_use_same_fields(env, layout_key):
    definition = module_layout_catalog.get_module_layout_definition(layout_key)
    initial = env.service.query("layouts.read", {"layout_key": layout_key})
    context = web._module_layout_context(req(env, {}), env.db, definition=definition)
    assert initial["show_more_index"] == context["show_more_index"]
    assert initial["revision"] == context["layout_revision"]
    fields, offset = [], "0"
    while offset:
        page = env.service.query("layouts.read", {"layout_key": layout_key, "offset": offset})
        fields.extend(field["key"] for field in page["fields"])
        offset = page["next_offset"]
    assert fields == [field.key for field in context["fields"]]
    assert not env.db.new and not env.db.dirty
    order = ["__show_more__", *reversed(fields)]
    data = {"order_json": json.dumps(order), "expected_revision": initial["revision"]}
    response = asyncio.run(web.update_module_layout(layout_key, req(env, data), env.db))
    assert response.status_code == 303
    updated = env.service.query("layouts.read", {"layout_key": layout_key})
    assert json.loads(updated["order_json"]) == order and updated["show_more_index"] == 0
    env.service.execute("layouts.update", {"layout_key": layout_key, "order_json": initial["order_json"], "expected_revision": updated["revision"]})
    assert env.service.query("layouts.read", {"layout_key": layout_key})["revision"] == initial["revision"]


def test_layout_list_future_fields_and_stale_revision(env, monkeypatch):
    assert {item["layout_key"] for item in env.service.query("layouts.list", {})["items"]} == set(LAYOUT_KEYS)
    key = "lead-fields"
    first = env.service.query("layouts.read", {"layout_key": key})
    definition = module_layout_catalog.get_module_layout_definition(key)
    new_field = module_layout_catalog.ModuleLayoutField("future_field", "Future field", "Text")
    monkeypatch.setitem(module_layout_catalog._LAYOUTS, key, replace(definition, fields=(*definition.fields, new_field)))
    second = env.service.query("layouts.read", {"layout_key": key})
    assert "future_field" in json.loads(second["order_json"])
    with pytest.raises(HubOperationError, match="inzwischen"):
        env.service.execute("layouts.update", {"layout_key": key, "order_json": first["order_json"], "expected_revision": first["revision"]})
    env.service.execute("layouts.update", {"layout_key": key, "order_json": second["order_json"], "expected_revision": second["revision"]})
    env.db.rollback()
    assert env.db.scalar(select(ModuleLayout)) is None


@pytest.mark.parametrize("bad_order", ['[]', '{}', '[null]', '["unknown"]', '["__show_more__","__show_more__"]', 'x' * 25001])
def test_invalid_layout_does_not_overwrite(env, bad_order):
    key = "finance-offer-fields"
    before = env.service.query("layouts.read", {"layout_key": key})
    with pytest.raises(ValueError):
        env.service.execute("layouts.update", {"layout_key": key, "order_json": bad_order})
    assert env.service.query("layouts.read", {"layout_key": key}) == before
    assert not env.db.new and not env.db.dirty


def test_layout_rejects_stale_human_and_agent_submissions(env, monkeypatch):
    key = "finance-offer-fields"
    before = env.service.query("layouts.read", {"layout_key": key})
    new_order = json.dumps(list(reversed(json.loads(before["order_json"]))))
    env.service.execute("layouts.update", {"layout_key": key, "order_json": new_order, "expected_revision": before["revision"]})
    env.db.commit()
    data = {"order_json": before["order_json"], "expected_revision": before["revision"]}
    with pytest.raises(HubOperationError, match="inzwischen"):
        env.service.execute("layouts.update", {"layout_key": key, **data})
    captured = {}
    def render(request, template, context, status_code=200):
        captured.update(context)
        return SimpleNamespace(status_code=status_code)
    monkeypatch.setattr(web.templates, "TemplateResponse", render)
    response = asyncio.run(web.update_module_layout(key, req(env, data), env.db))
    assert response.status_code == 400 and "inzwischen" in captured["error"]
    assert json.loads(env.service.query("layouts.read", {"layout_key": key})["order_json"]) == json.loads(new_order)


def test_layout_is_admin_only_even_with_settings_permission(env, monkeypatch):
    monkeypatch.setattr(env.access.__class__, "can", lambda *args, **kwargs: True)
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    for key, data in [("layouts.list", {}), ("layouts.read", {"layout_key": "lead-fields"})]:
        with pytest.raises(HubOperationError, match="Administratoren"):
            limited.query(key, data)
    with pytest.raises(HubOperationError):
        limited.execute("layouts.update", {"layout_key": "lead-fields", "order_json": "[]"})
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: env.sales)
    with pytest.raises(HTTPException) as error:
        web._module_layout_context(req(env, {}), env.db, definition=module_layout_definitions()[0])
    assert error.value.status_code == 403


def test_suggestions_share_filtered_native_search_before_limit(crm_env):
    e = crm_env
    e.own.name = "Match visible"
    for index in range(12):
        e.db.add(Customer(name=f"Match hidden {index}"))
    e.db.commit()
    request = SimpleNamespace(state=SimpleNamespace(hub_user=e.sales))
    result = e.limited.query("customers.suggestions", {"query": "Match"})
    assert [item["id"] for item in result["suggestions"]] == [e.own.id]
    assert result == web.customer_suggestions(request, e.db, q="Match")
    assert len(e.service.query("customers.suggestions", {"query": "Match"})["suggestions"]) == 7
    assert e.limited.query("customers.suggestions", {"query": "hidden"})["suggestions"] == []
    assert not e.db.new and not e.db.dirty


def test_case_drawer_and_catalog_enforce_parent_and_scope(crm_env, monkeypatch):
    e = crm_env
    own, hidden = case(e, e.own), case(e, e.hidden)
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: e.sales)
    monkeypatch.setattr(web, "_case_create_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(web.templates, "TemplateResponse", lambda request, template, context: context)
    request = SimpleNamespace(state=SimpleNamespace(hub_user=e.sales))
    detail = web.customer_case_edit_compose(e.own.id, own.id, request, e.db)
    assert detail["case_detail"].case.id == own.id
    assert e.limited.query("cases.read", {"case_id": str(own.id), "customer_id": str(e.own.id)})["customer_id"] == str(e.own.id)
    for customer, row in [(e.own, hidden), (e.hidden, hidden), (e.hidden, own)]:
        with pytest.raises(HubOperationError):
            e.limited.query("cases.read", {"case_id": str(row.id), "customer_id": str(customer.id)})
        with pytest.raises(HTTPException) as error:
            web.customer_case_edit_compose(customer.id, row.id, request, e.db)
        assert error.value.status_code == 404


@pytest.mark.parametrize("kind", PDF_KINDS)
def test_pdf_status_download_and_artifacts_share_loader_and_rights(env, kind, monkeypatch):
    result = env.service.execute(f"finance.{kind}.create", values(env, kind))
    env.db.commit()
    query = {"kind": kind, "record_id": str(result.record_id)}
    state = env.service.query("finance.pdf.status", query)
    assert state["status"] == "queued"
    assert web.finance_generated_pdf_status(kind, result.record_id, req(env, {}), env.db) == {"status": "queued"}
    with pytest.raises(HubOperationPending):
        env.service.load_artifact(state["artifact_ref"])
    row = env.db.scalar(select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.document_type == kind, HubFinanceGeneratedPdf.document_id == result.record_id))
    row.status, row.filename, row.byte_size = "ready", "test.pdf", 9
    env.db.commit()
    calls = []
    def load(self, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(filename="test.pdf", content_type="application/pdf"), b"%PDF-test"
    monkeypatch.setattr(HubFinancePdfService, "load", load)
    response = web.finance_generated_pdf_preview(kind, result.record_id, req(env, {}), env.db)
    assert response.body == env.service.load_artifact(state["artifact_ref"]).content == env.service.load_artifact(result.outputs["artifact_ref"]).content
    assert response.headers["cache-control"] == "private, no-store"
    assert "test.pdf" in response.headers["content-disposition"]
    assert all(item == {"document_type": kind, "document_id": result.record_id} for item in calls)
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        limited.query("finance.pdf.status", query)
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    env.db.commit()
    assert limited.load_artifact(state["artifact_ref"]).content == response.body
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=None, team_id=None)
    env.db.commit()
    with pytest.raises(HubOperationError):
        limited.load_artifact(state["artifact_ref"])
    assert not env.db.new and not env.db.dirty


def test_pdf_missing_original_and_new_generated_never_use_stale_fallback(env, monkeypatch):
    result = env.service.execute("finance.invoices.create", values(env, "invoices"))
    row = env.db.scalar(select(HubFinanceGeneratedPdf))
    env.db.delete(row)
    env.db.commit()
    query = {"kind": "invoices", "record_id": str(result.record_id)}
    assert env.service.query("finance.pdf.status", query)["status"] == "missing"
    original = HubFinanceInvoicePdf(invoice_id=result.record_id, filename="original.pdf", byte_size=9, storage_key="test", imported_at=datetime.now(UTC))
    env.db.add(original)
    env.db.commit()
    monkeypatch.setattr(HubFinanceDocumentService, "load_invoice_pdf", lambda *args, **kwargs: (original, b"%PDF-test"))
    state = env.service.query("finance.pdf.status", query)
    assert state["status"] == "ready" and state["source"] == "original"
    assert env.service.load_artifact(state["artifact_ref"]).content == web.finance_invoice_pdf_preview(result.record_id, req(env, {}), env.db).body
    env.service.execute("finance.pdf.generate", query)
    row = env.db.scalar(select(HubFinanceGeneratedPdf))
    row.status = "failed"
    env.db.commit()
    assert env.service.query("finance.pdf.status", query)["status"] == "failed"
    with pytest.raises(HubOperationError):
        load_pdf(env.service, "invoices", result.record_id, source="available", wait=True)
    assert env.service.load_artifact(state["artifact_ref"]).content == b"%PDF-test"


def test_pdf_text_is_paged_readonly_and_ocr_flag_retained(env, monkeypatch):
    result = env.service.execute("finance.offers.create", values(env, "offers"))
    row = env.db.scalar(select(HubFinanceGeneratedPdf))
    row.status = "ready"
    env.db.commit()
    monkeypatch.setattr(HubFinancePdfService, "load", lambda *args, **kwargs: (SimpleNamespace(filename="test.pdf"), b"%PDF-test"))
    monkeypatch.setattr(hub_operation_pdf_reads, "extract_agent_file", lambda **kwargs: SimpleNamespace(name="test.pdf", text="a" * 14000, used_ocr=True))
    def unexpected(*args, **kwargs):
        pytest.fail("PDF read must not generate, import or commit")
    monkeypatch.setattr(HubFinancePdfService, "queue", unexpected)
    monkeypatch.setattr(env.db, "commit", unexpected)
    text, offset = "", "0"
    while offset:
        page = env.service.query("finance.pdf.read", {"kind": "offers", "record_id": str(result.record_id), "text_offset": offset})
        assert len(page["text"]) <= 6000 and page["used_ocr"]
        text += page["text"]
        offset = page["next_text_offset"]
    assert text == "a" * 14000 and not env.db.new and not env.db.dirty


@pytest.mark.parametrize("reference", ["offers/0/generated", "offers/9999/generated", "articles/1/generated", "offers/1/remote", "offers/1/generated/extra"])
def test_pdf_artifact_invalid_inputs_fail_closed(env, reference):
    with pytest.raises(HubOperationError):
        env.service.load_artifact("finance.pdf.file:" + reference)
