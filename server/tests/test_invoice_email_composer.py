import asyncio
import json
import inspect
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from starlette.datastructures import FormData
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_finance_documents import HubFinanceInvoice
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.models.hub_invoice_email_batch import HubInvoiceEmailBatchItem
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.zoho_email_template import ZohoEmailTemplate
from app.services.customer_communications import CustomerCommunicationService
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.hub_email_composition import mailbox_for
from app.services.hub_invoice_email_delivery import invoice_email_deliveries
from app.services.hub_operation_invoice_email import SEND_FIELDS
from app.services.hub_operations import HubArtifact, HubOperationError, HubOperationService, agent_operations
from test_hub_finance_operations import env


@pytest.fixture
def prepared(env, monkeypatch, tmp_path):
    env.contact.encrypted_profile_json = env.cipher.encrypt(json.dumps({"fields": {"Name": "Test contact", "E-Mail": "test@example.test"}}))
    invoice = HubFinanceInvoice(customer_id=env.customer.id, contact_id=env.contact.id, invoice_number="RE-TEST",
        encrypted_fields_json=env.cipher.encrypt(json.dumps({"status": "open", "invoice_date": "2026-09-25", "due_date": "2026-10-01", "currency": "EUR"})))
    env.db.add(invoice)
    env.db.flush()
    env.db.add(HubFinanceInvoicePdf(invoice_id=invoice.id, filename="RE-TEST.pdf", storage_key="a" * 48,
        byte_size=20, content_type="application/pdf", is_zugferd=True, imported_at=datetime.now(UTC)))
    env.db.add(ZohoEmailTemplate(zoho_template_id="invoice-test", module="Accounts", is_active=True, zoho_synced_at=datetime.now(UTC),
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"name": "Rechnungen senden", "subject": "Rechnung ${Invoice.Number}",
        "content": "<p>Guten Tag ${Contact.Name}, fällig ${Invoice.DueDate}.</p>"}))))
    env.db.commit()
    storage = EmailAttachmentStorage(root=tmp_path / "attachments", cipher=env.cipher, min_free_bytes=0)
    monkeypatch.setattr("app.services.customer_communications.EmailAttachmentStorage", lambda **kwargs: storage)
    monkeypatch.setattr("app.services.hub_operation_invoice_email.FinanceInvoicePdfStorage.load", lambda *_: b"%PDF-invoice-test")
    sent = []
    def send(_self, **kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id="test@example.test", sent_at=datetime.now(UTC))
    monkeypatch.setattr("app.services.hub_mailbox_transport.HubMailboxTransportService.send", send)
    result = env.service.execute("finance.invoices.email.prepare", {"record_id": str(invoice.id)})
    env.db.commit()
    context = mailbox_for(env.service).get_draft_compose_context(draft_id=result.record_id)
    data = {field: str(context.get(field) or "") for field in SEND_FIELDS}
    data.update(recipient_customer_id=str(invoice.customer_id), recipient_key=context["recipient"]["key"],
                retained_attachment_ids=json.dumps([file["id"] for file in context["attachments"]]))
    return SimpleNamespace(env=env, invoice=invoice, context=context, data=data, sent=sent, storage=storage)


def test_prepare_only_creates_editable_draft_with_pdf(prepared):
    p = prepared
    assert not p.sent
    assert p.context["subject"] == "Rechnung RE-TEST"
    assert "Test contact" in p.context["content"] and "01.10.2026" in p.context["content"]
    assert p.context["attachments"][0]["filename"] == "RE-TEST.pdf"
    assert p.context["invoice_id"] == p.invoice.id
    assert p.env.db.query(HubInvoiceEmailBatchItem).count() == 0
    assert invoice_email_deliveries(p.env.db, [p.invoice.id])[p.invoice.id].status == "not_sent"
    assert "finance.invoices.email.send" not in {op.key for op in agent_operations()}


def test_send_exact_edited_values_one_pdf_and_update_status(prepared):
    p = prepared
    before = p.invoice.encrypted_fields_json
    data = {**p.data, "subject": "Edited subject", "content": "<p>My edited invoice message</p>", "cc_emails": "copy@example.test"}
    result = p.env.service.execute("finance.invoices.email.send", data)
    assert result.href.startswith(f"/finance/invoices/{p.invoice.id}?")
    assert len(p.sent) == 1
    assert p.sent[0]["subject"] == "Edited subject"
    assert "My edited invoice message" in p.sent[0]["html_content"]
    assert len(p.sent[0]["attachments"]) == 1
    assert p.sent[0]["attachments"][0].content == b"%PDF-invoice-test"
    assert p.sent[0]["cc_recipients"][0][1] == "copy@example.test"
    summary = invoice_email_deliveries(p.env.db, [p.invoice.id])[p.invoice.id]
    assert summary.status == "sent" and summary.sent_at is not None
    assert p.env.db.get(HubMailboxEmail, p.context["draft_id"]) is None
    assert p.env.db.scalar(select(CustomerZohoEmail)).customer_id == p.invoice.customer_id
    assert p.invoice.encrypted_fields_json == before
    with pytest.raises(ValueError):
        p.env.service.execute("finance.invoices.email.send", data)
    assert len(p.sent) == 1


@pytest.mark.parametrize("change", ["attachment", "invoice", "pdf", "schedule", "customer", "empty", "permission"])
def test_reject_invalid_or_stale_draft_before_smtp(prepared, change):
    p = prepared
    data = dict(p.data)
    service = p.env.service
    if change == "attachment": data["retained_attachment_ids"] = "[]"
    if change == "invoice": p.invoice.encrypted_fields_json = p.env.cipher.encrypt(json.dumps({"status": "paid"}))
    if change == "pdf": p.env.db.scalar(select(HubFinanceInvoicePdf)).storage_key = "b" * 48
    if change == "schedule": data["scheduled_at"] = "2026-10-01T10:00"
    if change == "customer": data["recipient_customer_id"] = str(p.env.hidden.id)
    if change == "empty": data["content"] = ""
    if change == "permission": service = HubOperationService(db=p.env.db, cipher=p.env.cipher, actor=p.env.sales.username)
    with pytest.raises(ValueError): service.execute("finance.invoices.email.send", data)
    assert not p.sent and p.env.db.query(HubInvoiceEmailBatchItem).count() == 0


def test_known_failure_retains_draft_and_can_be_retried(prepared, monkeypatch):
    p = prepared
    original = CustomerCommunicationService.send_email
    monkeypatch.setattr(CustomerCommunicationService, "send_email", lambda *args, **kwargs: SimpleNamespace(success=False))
    with pytest.raises(ValueError, match="fehlgeschlagen"):
        p.env.service.execute("finance.invoices.email.send", p.data)
    p.env.db.rollback()
    assert invoice_email_deliveries(p.env.db, [p.invoice.id])[p.invoice.id].status == "failed"
    assert p.env.db.get(HubMailboxEmail, p.context["draft_id"]) is not None
    monkeypatch.setattr(CustomerCommunicationService, "send_email", original)
    p.env.service.execute("finance.invoices.email.send", p.data)
    assert invoice_email_deliveries(p.env.db, [p.invoice.id])[p.invoice.id].status == "sent"


def test_unknown_outcome_is_durable_and_not_retried(prepared, monkeypatch):
    p = prepared
    def failed(*args, **kwargs): raise RuntimeError("Lost SMTP response")
    monkeypatch.setattr(CustomerCommunicationService, "send_email", failed)
    with pytest.raises(ValueError, match="unklar"):
        p.env.service.execute("finance.invoices.email.send", p.data)
    p.env.db.rollback()
    summary = invoice_email_deliveries(p.env.db, [p.invoice.id])[p.invoice.id]
    assert summary.status == "uncertain" and summary.sent_at is None
    with pytest.raises(ValueError, match="unklar"):
        p.env.service.execute("finance.invoices.email.send", p.data)
    assert p.env.db.query(HubInvoiceEmailBatchItem).count() == 1


def test_duplicate_submit_during_smtp_is_blocked(prepared, monkeypatch):
    p = prepared
    def send(*args, **kwargs):
        with pytest.raises(ValueError, match="läuft"):
            p.env.service.execute("finance.invoices.email.send", p.data)
        return SimpleNamespace(success=True)
    monkeypatch.setattr(CustomerCommunicationService, "send_email", send)
    p.env.service.execute("finance.invoices.email.send", p.data)
    assert p.env.db.query(HubInvoiceEmailBatchItem).count() == 1


def test_invoice_metadata_survives_normal_draft_save_and_reopen(prepared):
    p = prepared
    data = {**p.data, "content": "<p>Saved edits</p>"}
    p.env.service.execute("emails.drafts.save", data)
    p.env.db.commit()
    context = mailbox_for(p.env.service).get_draft_compose_context(draft_id=p.context["draft_id"])
    assert context["invoice_id"] == p.invoice.id
    assert "Saved edits" in context["content"]
    assert len(context["attachments"]) == 1 and not p.sent


def test_generic_send_adapters_keep_invoice_tracking_and_latest_posted_content(prepared):
    p = prepared
    class Req:
        headers = {"accept": "application/json"}
        query_params = {}
        async def form(self): return FormData({**p.data, "content": "<p>Latest posted version</p>"})
    response = asyncio.run(web._dispatch_invoice_draft(Req(), p.env.db, p.env.user, str(p.context["draft_id"]), ()))
    assert response.status_code == 200
    assert "/finance/invoices/" in json.loads(response.body)["redirect_url"]
    assert "Latest posted version" in p.sent[0]["html_content"]


@pytest.mark.parametrize("route", ["direct", "customer"])
def test_actual_manual_routes_use_invoice_operation(prepared, route):
    p = prepared
    data = {**p.data, "subject": "Submitted from normal editor"}
    class Req:
        headers = {"accept": "application/json"}
        query_params = {}
        async def form(self): return FormData(data)
    function = web.send_direct_mailbox_email if route == "direct" else web.send_customer_communication_email
    kwargs = {key: value for key, value in data.items() if key in inspect.signature(function).parameters}
    if route == "customer": kwargs["customer_id"] = p.invoice.customer_id
    response = asyncio.run(function(request=Req(), db=p.env.db, **kwargs))
    assert response.status_code == 200 and len(p.sent) == 1
    assert p.sent[0]["subject"] == data["subject"]
    assert invoice_email_deliveries(p.env.db, [p.invoice.id])[p.invoice.id].status == "sent"


def test_customer_route_rejects_foreign_customer(prepared):
    p = prepared
    class Req:
        headers = {"accept": "application/json"}
        query_params = {}
        async def form(self): return FormData(p.data)
    with pytest.raises(ValueError, match="anderen Kunden"):
        asyncio.run(web._dispatch_invoice_draft(Req(), p.env.db, p.env.user, str(p.context["draft_id"]), (), customer_id=p.env.hidden.id))
    assert not p.sent


def test_changed_address_and_additional_file_preserve_invoice_delivery(prepared):
    p = prepared
    service = HubOperationService(db=p.env.db, cipher=p.env.cipher, actor=p.env.user.username,
        input_files=(HubArtifact(filename="extra.txt", content=b"Additional information", content_type="text/plain"),))
    service.execute("finance.invoices.email.send", {**p.data, "recipient_key": "", "recipient_email": "changed@example.test"})
    assert len(p.sent) == 1
    assert [file.content for file in p.sent[0]["attachments"]] == [b"%PDF-invoice-test", b"Additional information"]
    assert invoice_email_deliveries(p.env.db, [p.invoice.id])[p.invoice.id].status == "sent"


@pytest.mark.parametrize("missing", ["pdf", "contact", "template"])
def test_prepare_missing_requirements_cannot_create_empty_or_sendable_draft(prepared, missing):
    p = prepared
    before = p.env.db.query(HubMailboxEmail).count()
    if missing == "pdf": p.env.db.delete(p.env.db.scalar(select(HubFinanceInvoicePdf)))
    if missing == "contact": p.invoice.contact_id = None
    if missing == "template": p.env.db.delete(p.env.db.scalar(select(ZohoEmailTemplate)))
    p.env.db.commit()
    with pytest.raises(ValueError):
        p.env.service.execute("finance.invoices.email.prepare", {"record_id": str(p.invoice.id)})
    assert p.env.db.query(HubMailboxEmail).count() == before and not p.sent


def test_prepare_endpoint_csrf_and_template_browser_fixtures(prepared, monkeypatch):
    p = prepared
    app = create_app()
    request = Request({"type": "http", "method": "GET", "path": f"/finance/invoices/{p.invoice.id}", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("hub.test", 80), "session": {}, "app": app, "router": app.router})
    request.state.hub_user = p.env.user
    page = web.finance_document_detail_page("invoices", p.invoice.id, request, p.env.db).body.decode()
    assert 'data-invoice-email-compose="' + str(p.invoice.id) + '"' in page
    assert page.index('data-customer-edit-open') < page.index('data-invoice-email-compose')
    from fastapi import HTTPException
    def deny(*args): raise HTTPException(403)
    monkeypatch.setattr(web, "require_csrf", deny)
    with pytest.raises(HTTPException): web.prepare_finance_invoice_email(p.invoice.id, request, p.env.db)
    assert not p.sent
    directory = os.environ.get("HUB_INVOICE_COMPOSER_ARTIFACT_DIR")
    if directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root / "invoice.html").write_text(page, encoding="utf-8")
        (root / "data.json").write_text(json.dumps({"invoice": p.invoice.id, "context": p.context}), encoding="utf-8")
