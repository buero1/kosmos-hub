import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from mailbox_fixture_helpers import mailbox_account

from app.api.routes import web
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_pdf_template import HubPdfTemplate
from app.models.hub_user import HubUser
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_finance_documents import FINANCE_DOCUMENT_MODULES
from app.services.hub_finance_operations_shared import (
    FINANCE_KINDS, PDF_KINDS, finance_invoice_page, fields_for, form_input, model_for, prefix_for,
)
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_operations import HubOperationError, HubOperationPending, HubOperationService, get_operation


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        cipher = get_secret_cipher()
        user = HubUser(username="admin", password_hash="x", role="admin")
        sales = HubUser(username="sales", password_hash="x", role="sales")
        customer = Customer(name="Finance test")
        hidden = Customer(name="Hidden customer")
        db.add_all([user, sales, customer, hidden])
        contact = CustomerContact(customer=customer, encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Name": "Test contact", "Email": "test@example.test"}})))
        db.add(contact)
        access = HubAccessControlService(db=db)
        access.ensure_defaults()
        mailbox_account(db, cipher)
        db.commit()
        monkeypatch.setattr(web, "_require_hub_admin", lambda request: user)
        monkeypatch.setattr(web, "require_csrf", lambda *args: None)
        monkeypatch.setattr(web, "get_csrf_token", lambda *args: "test")
        monkeypatch.setattr(web, "write_audit_log", lambda *args, **kwargs: None)
        yield SimpleNamespace(db=db, cipher=cipher, user=user, sales=sales, customer=customer, hidden=hidden,
                              contact=contact, access=access, service=HubOperationService(db=db, cipher=cipher, actor="admin"), monkeypatch=monkeypatch)
    engine.dispose()


def values(env, kind):
    if kind == "articles":
        return {"article_field__name": "Website", "article_field__sku": "WEB", "article_field__net_price": "100", "article_field__revenue_account": "8400"}
    prefix = prefix_for(kind)
    data = {"customer_id": str(env.customer.id), "contact_id": str(env.contact.id), f"{prefix}_line__0__name": "Website", f"{prefix}_line__0__unit_price": "100", f"{prefix}_line__1__name": "Support", f"{prefix}_line__1__unit_price": "25"}
    if kind == "orders":
        for field in fields_for(kind):
            if field.required and field.options and field.key != "status":
                data[f"document_field__{field.key}"] = field.options[0][0]
        data["document_field__order_name"] = "Website order"
    if kind == "dunnings":
        invoice = env.service.execute("finance.invoices.create", values(env, "invoices"))
        data["linked_record_id"] = str(invoice.record_id)
    if kind == "recurring-invoices":
        data["document_field__name"] = "Recurring support"
    return data


def read(env, kind, record_id, **extra):
    return env.service.query("finance.read", {"kind": kind, "record_id": str(record_id), **extra})


def req(env, data):
    class Request:
        state = SimpleNamespace(hub_user=env.user)

        async def form(self):
            return FormData(data)
    return Request()


@pytest.mark.parametrize("kind", FINANCE_KINDS)
def test_create_read_partial_update_and_delete(env, kind):
    result = env.service.execute(f"finance.{kind}.create", values(env, kind))
    before = read(env, kind, result.record_id)
    prefix = prefix_for(kind)
    update = {"record_id": str(result.record_id)}
    if kind == "articles":
        update["article_field__description"] = "Updated"
    else:
        update[f"{prefix}_line__0__unit_price"] = "130"
    env.service.execute(f"finance.{kind}.update", update)
    after = read(env, kind, result.record_id)
    if kind == "articles":
        assert after["input_values"]["article_field__description"] == "Updated"
        assert after["input_values"]["article_field__name"] == "Website"
    else:
        assert after["input_values"] == before["input_values"]
        assert after["line_count"] == 2
        assert after["lines"][0]["input_values"][f"{prefix}_line__0__unit_price"] == "130.00"
        assert after["lines"][1] == before["lines"][1]
        assert after["total_gross"] == "184.45"
        assert after["lines"][0]["input_values"][f"{prefix}_line__0__unit"] == ""
    env.service.execute(f"finance.{kind}.delete", {"record_id": str(result.record_id)})
    assert env.db.get(model_for(kind), result.record_id) is None


@pytest.mark.parametrize("kind", ("offers", *FINANCE_DOCUMENT_MODULES))
def test_lines_patch_delete_replace_and_empty_replace_are_explicit(env, kind):
    created = env.service.execute(f"finance.{kind}.create", values(env, kind))
    prefix = prefix_for(kind)
    target = {"record_id": str(created.record_id)}
    env.service.execute(f"finance.{kind}.update", {**target, f"{prefix}_line__0__delete": "true", f"{prefix}_line__2__name": "New", f"{prefix}_line__2__unit_price": "50"})
    detail = read(env, kind, created.record_id)
    assert detail["line_count"] == 2
    assert detail["lines"][0]["input_values"][f"{prefix}_line__0__name"] == "Support"
    env.service.execute(f"finance.{kind}.update", {**target, "lines_mode": "replace", f"{prefix}_line__0__name": "Only", f"{prefix}_line__0__unit_price": "60"})
    assert read(env, kind, created.record_id)["line_count"] == 1
    env.service.execute(f"finance.{kind}.update", {**target, "lines_mode": "replace"})
    assert read(env, kind, created.record_id)["line_count"] == 0


@pytest.mark.parametrize("kind", FINANCE_KINDS)
def test_human_routes_and_catalog_share_crud(env, kind):
    data = values(env, kind)
    request = req(env, data)
    background = BackgroundTasks()
    if kind == "articles":
        response = asyncio.run(web.create_finance_article_page(request, env.db))
    elif kind == "offers":
        response = asyncio.run(web.create_finance_offer_page(request, background, env.db))
    else:
        response = asyncio.run(web.create_finance_document_page(kind, request, background, env.db))
    assert response.status_code == 303
    record_id = int(response.headers["location"].rsplit("/", 1)[1])
    data = {**get_operation(f"finance.{kind}.create").apply_defaults(data)}
    prefix = prefix_for(kind)
    data[f"{prefix}_field__status"] = {"articles": "inactive", "dunnings": "dunning", "recurring-invoices": "paused"}.get(kind, "draft")
    request = req(env, data)
    if kind == "articles":
        response = asyncio.run(web.update_finance_article_fields(record_id, request, env.db))
    elif kind == "offers":
        response = asyncio.run(web.update_finance_offer_fields(record_id, request, background, env.db))
    else:
        response = asyncio.run(web.update_finance_document_fields(kind, record_id, request, background, env.db))
    assert "error" not in response.headers["location"]
    detail = read(env, kind, record_id)
    assert detail["input_values"][f"{prefix}_field__status"] == data[f"{prefix}_field__status"]
    if kind == "articles":
        response = asyncio.run(web.delete_finance_article(record_id, request, env.db))
    elif kind == "offers":
        response = asyncio.run(web.delete_finance_offer(record_id, request, env.db))
    else:
        response = asyncio.run(web.delete_finance_document(kind, record_id, request, env.db))
    assert response.status_code == 303 and "deleted=true" in response.headers["location"]


@pytest.mark.parametrize("kind", ("offers", *FINANCE_DOCUMENT_MODULES))
def test_scope_denies_read_mutate_pdf_and_list(env, kind):
    result = env.service.execute(f"finance.{kind}.create", values(env, kind))
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor="sales")
    assert limited.query("finance.list", {"kind": kind})["items"] == []
    for action in ("read", "update", "delete"):
        with pytest.raises(HubOperationError):
            if action == "read":
                limited.query("finance.read", {"kind": kind, "record_id": str(result.record_id)})
            else:
                limited.execute(f"finance.{kind}.{action}", {"record_id": str(result.record_id)})
    if kind in PDF_KINDS:
        with pytest.raises(HubOperationError):
            limited.load_artifact(result.outputs["artifact_ref"])
    env.monkeypatch.setattr(web, "_require_hub_admin", lambda request: env.sales)
    with pytest.raises(HTTPException) as error:
        web._finance_read(req(env, {}), env.db, kind, result.record_id)
    assert error.value.status_code == 404
    assert finance_invoice_page(limited, 1).total_count == 0


def test_catalog_optional_fields_defaults_and_recurring_contract(env):
    for kind in FINANCE_KINDS:
        create = get_operation(f"finance.{kind}.create").input_contract()
        update = get_operation(f"finance.{kind}.update").input_contract()
        for field in fields_for(kind):
            name = f"{prefix_for(kind)}_field__{field.key}"
            if field.read_only:
                assert name not in create
            elif name in create:
                assert create[name]["required"] == field.required
                assert update[name]["required"] is False
    assert get_operation("finance.invoices.create").input_contract()["document_field__payment_terms"]["required"] is False
    assert get_operation("finance.recurring-invoices.create").input_contract()["document_field__status"]["default"] == "active"
    assert get_operation("finance.articles.create").input_contract()["article_field__tax_rate"]["default"] == "19"


def test_foreign_contact_and_source_document_rejected_before_commit(env):
    other_contact = CustomerContact(customer=env.hidden, encrypted_profile_json=env.cipher.encrypt(json.dumps({"fields": {}})))
    env.db.add(other_contact)
    env.db.flush()
    with pytest.raises(ValueError):
        env.service.execute("finance.invoices.create", {**values(env, "invoices"), "contact_id": str(other_contact.id)})
    invoice = env.service.execute("finance.invoices.create", values(env, "invoices"))
    with pytest.raises(ValueError):
        env.service.execute("finance.dunnings.create", {**values(env, "invoices"), "customer_id": str(env.hidden.id), "contact_id": str(other_contact.id), "linked_record_id": str(invoice.record_id)})


def test_read_options_never_initializes_templates_and_respects_scope(env):
    assert not env.db.scalars(select(HubPdfTemplate)).all()
    assert env.service.query("finance.options", {"kind": "offers", "category": "pdf_templates"})["items"] == []
    assert not env.db.new and not env.db.dirty
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor="sales")
    assert limited.query("finance.options", {"kind": "offers", "category": "customers"})["items"] == []
    result = env.service.query("finance.options", {"kind": "offers", "category": "contacts", "customer_id": str(env.customer.id)})
    assert result["items"][0]["id"] == str(env.contact.id)


@pytest.mark.parametrize("kind", PDF_KINDS)
def test_pdf_artifact_pending_ready_and_regenerate(env, kind):
    result = env.service.execute(f"finance.{kind}.create", values(env, kind))
    with pytest.raises(HubOperationPending):
        env.service.load_artifact(result.outputs["artifact_ref"])
    state = env.db.scalar(select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.document_type == kind, HubFinanceGeneratedPdf.document_id == result.record_id))
    state.status = "ready"
    env.db.flush()
    env.monkeypatch.setattr(HubFinancePdfService, "load", lambda *args, **kwargs: (SimpleNamespace(filename="test.pdf"), b"%PDF-test"))
    assert env.service.load_artifact(result.outputs["artifact_ref"]).content == b"%PDF-test"
    regenerated = env.service.execute("finance.pdf.generate", {"kind": kind, "record_id": str(result.record_id)})
    assert regenerated.background_token
    assert regenerated.outputs["artifact_ref"] == result.outputs["artifact_ref"]


def test_dunning_prefill_and_recurring_partial_update(env):
    invoice = env.service.execute("finance.invoices.create", values(env, "invoices"))
    draft = env.service.query("finance.dunning_source", {"record_id": str(invoice.record_id)})
    dunning = env.service.execute("finance.dunnings.create", draft["input_values"])
    assert read(env, "dunnings", dunning.record_id)["line_count"] == 2
    recurring = env.service.execute("finance.recurring-invoices.create", {**values(env, "recurring-invoices"), "document_field__interval_unit": "custom", "document_field__custom_interval_count": "3", "document_field__custom_interval_unit": "week", "document_field__payment_due_count": "14", "document_field__payment_due_unit": "day"})
    before = read(env, "recurring-invoices", recurring.record_id)
    env.service.execute("finance.recurring-invoices.update", {"record_id": str(recurring.record_id), "document_field__name": "Renamed"})
    after = read(env, "recurring-invoices", recurring.record_id)
    for key in ("custom_interval_count", "custom_interval_unit", "payment_due_count", "payment_due_unit"):
        assert after["input_values"][f"document_field__{key}"] == before["input_values"][f"document_field__{key}"]


def test_pdf_cleanup_only_after_commit(env):
    from app.services.finance_generated_pdf_storage import FinanceGeneratedPdfStorage
    result = env.service.execute("finance.offers.create", values(env, "offers"))
    pdf = env.db.scalar(select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.document_type == "offers"))
    pdf.storage_key = "test-stored-pdf"
    env.db.commit()
    removed = []
    env.monkeypatch.setattr(FinanceGeneratedPdfStorage, "remove", lambda self, key: removed.append(key))
    with env.db.begin_nested() as transaction:
        env.service.execute("finance.offers.delete", {"record_id": str(result.record_id)})
        assert not removed
        transaction.rollback()
    env.db.commit()
    assert not removed
    assert env.db.get(model_for("offers"), result.record_id) is not None
    env.service.execute("finance.offers.delete", {"record_id": str(result.record_id)})
    assert not removed
    env.db.commit()
    assert removed == ["test-stored-pdf"]


def test_read_line_pagination_and_search(env):
    data = values(env, "offers")
    for index in range(2, 26):
        data.update({f"offer_line__{index}__name": f"Item {index}", f"offer_line__{index}__unit_price": "1"})
    offer = env.service.execute("finance.offers.create", data)
    first = read(env, "offers", offer.record_id)
    second = read(env, "offers", offer.record_id, line_offset=first["next_line_offset"])
    assert len(first["lines"]) == 20 and len(second["lines"]) == 6
    assert second["lines"][0]["index"] == 20
    assert second["next_line_offset"] == ""
    assert env.service.query("finance.list", {"kind": "offers", "customer_id": str(env.customer.id), "query": "Finance test"})["total"] == 1


def test_readonly_and_unknown_fields_fail_and_no_auto_email_operation(env):
    invoice = env.service.execute("finance.invoices.create", values(env, "invoices"))
    for key in ("document_field__invoice_number", "document_field__fake", "document_line__-1__name"):
        with pytest.raises(HubOperationError):
            env.service.execute("finance.invoices.update", {"record_id": str(invoice.record_id), key: "bad"})
    assert get_operation("finance.invoices.send") is None


def test_recurring_edit_retains_actual_next_run_cursor(env):
    result = env.service.execute("finance.recurring-invoices.create", values(env, "recurring-invoices"))
    record = env.db.get(model_for("recurring-invoices"), result.record_id)
    record.hub_next_run_on = date(2030, 1, 1)
    env.db.flush()
    env.service.execute("finance.recurring-invoices.update", {"record_id": str(record.id), "document_field__name": "New name"})
    assert record.hub_next_run_on == date(2030, 1, 1)


def test_long_text_can_be_read_completely_without_mutation(env):
    text = "Long position description. " * 600
    result = env.service.execute("finance.invoices.create", {**values(env, "invoices"), "document_line__0__description": text})
    env.db.commit()
    mutations = []

    def statement(connection, cursor, sql, parameters, context, many):
        if sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            mutations.append(sql)

    event.listen(env.db.bind, "before_cursor_execute", statement)
    try:
        detail = read(env, "invoices", result.record_id)
        key = "document_line__0__description"
        assert key in detail["truncated_fields"]
        chunks, offset = [], "0"
        while offset:
            page = read(env, "invoices", result.record_id, field=key, text_offset=offset)
            chunks.append(page["value"])
            offset = page["next_text_offset"]
        assert "".join(chunks) == text.strip()
        env.service.query("finance.list", {"kind": "invoices"})
        env.service.query("finance.options", {"kind": "invoices", "category": "pdf_templates"})
        env.service.query("finance.dunning_source", {"record_id": str(result.record_id)})
        assert not mutations
    finally:
        event.remove(env.db.bind, "before_cursor_execute", statement)


def test_invoice_pdf_can_be_used_for_an_unsent_email_draft(env, tmp_path):
    from app.models.hub_mailbox_email import HubMailboxEmail
    from app.services.email_attachment_storage import EmailAttachmentStorage

    result = env.service.execute("finance.invoices.create", values(env, "invoices"))
    pdf = env.db.scalar(select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.document_type == "invoices"))
    pdf.status = "ready"
    env.db.flush()
    env.monkeypatch.setattr(HubFinancePdfService, "load", lambda *args, **kwargs: (SimpleNamespace(filename="invoice.pdf"), b"%PDF-invoice"))
    initialize = EmailAttachmentStorage.__init__
    env.monkeypatch.setattr(EmailAttachmentStorage, "__init__", lambda self, **kwargs: initialize(self, root=tmp_path, cipher=env.cipher, min_free_bytes=0))
    # The draft operation uses the same artifact loader; it never invokes SMTP.
    draft = env.service.execute("emails.drafts.create", {"recipient_email": "test@example.test", "subject": "Invoice", "content": "<p>Attached.</p>", "customer_id": str(env.customer.id), "attachment_ref": result.outputs["artifact_ref"]})
    assert "folder=drafts" in draft.href
    assert env.db.get(HubMailboxEmail, draft.record_id) is not None
