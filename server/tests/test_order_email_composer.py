import asyncio
import inspect
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.datastructures import FormData
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_finance_documents import HubFinanceOrder
from app.models.hub_finance_order_pdf import HubFinanceOrderPdf
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.zoho_email_template import ZohoEmailTemplate
from app.services.customer_communications import CustomerCommunicationService
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.hub_access_control import permission_target
from app.services.hub_email_composition import mailbox_for
from app.services.hub_operation_invoice_email import SEND_FIELDS
from app.services.hub_operations import HubOperationService, agent_operations
from test_hub_finance_operations import env


@pytest.fixture
def prepared(env, monkeypatch, tmp_path):
    env.contact.encrypted_profile_json = env.cipher.encrypt(json.dumps({"fields": {"Name": "Test contact", "E-Mail": "test@example.test"}}))
    order = HubFinanceOrder(customer_id=env.customer.id, contact_id=env.contact.id, order_number="AU-TEST",
        encrypted_fields_json=env.cipher.encrypt(json.dumps({"status": "draft", "order_date": "2026-09-25", "currency": "EUR"})))
    env.db.add(order)
    env.db.flush()
    env.db.add(HubFinanceOrderPdf(order_id=order.id, filename="AU-TEST.pdf", storage_key="a" * 48,
        byte_size=20, imported_at=datetime.now(UTC)))
    env.db.add(ZohoEmailTemplate(zoho_template_id="order-test", module="Accounts", is_active=True, zoho_synced_at=datetime.now(UTC),
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"name": "Auftragsbestätigung", "hub_context_module": "orders",
        "subject": "Auftrag ${Order.Number}", "content": "<p>Guten Tag ${Contact.Name}, hier Ihr Auftrag.</p>"}))))
    env.db.commit()
    storage = EmailAttachmentStorage(root=tmp_path / "attachments", cipher=env.cipher, min_free_bytes=0)
    monkeypatch.setattr("app.services.customer_communications.EmailAttachmentStorage", lambda **kwargs: storage)
    monkeypatch.setattr("app.services.finance_invoice_pdf_storage.FinanceInvoicePdfStorage.load", lambda *_: b"%PDF-order-test")
    sent = []
    def send(_self, **kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id="test@example.test", sent_at=datetime.now(UTC))
    monkeypatch.setattr("app.services.hub_mailbox_transport.HubMailboxTransportService.send", send)
    result = env.service.execute("finance.orders.email.prepare", {"record_id": str(order.id)})
    env.db.commit()
    context = mailbox_for(env.service).get_draft_compose_context(draft_id=result.record_id)
    data = {field: str(context.get(field) or "") for field in SEND_FIELDS}
    data.update(recipient_customer_id=str(order.customer_id), recipient_key=context["recipient"]["key"],
                retained_attachment_ids=json.dumps([item["id"] for item in context["attachments"]]))
    return SimpleNamespace(env=env, order=order, context=context, data=data, sent=sent)


def test_prepares_template_customer_contact_and_pdf_without_sending(prepared):
    p = prepared
    assert not p.sent
    assert p.context["subject"] == "Auftrag AU-TEST"
    assert "Test contact" in p.context["content"]
    assert p.context["customer_id"] == p.order.customer_id
    assert p.context["order_id"] == p.order.id
    assert p.context["attachments"][0]["filename"] == "AU-TEST.pdf"
    assert "finance.orders.email.send" not in {op.key for op in agent_operations()}


@pytest.mark.parametrize("route", ["direct", "customer"])
@pytest.mark.parametrize("custom_address", [False, True])
def test_manual_routes_keep_edited_message_and_customer_link(prepared, route, custom_address):
    p = prepared
    before = p.order.encrypted_fields_json
    data = {**p.data, "subject": "Edited order subject", "content": "<p>My edited order</p>"}
    if custom_address:
        data.update(recipient_key="", recipient_email="another@example.test", recipient_customer_id="")
    class Req:
        headers = {"accept": "application/json"}
        query_params = {}
        async def form(self): return FormData(data)
    function = web.send_direct_mailbox_email if route == "direct" else web.send_customer_communication_email
    kwargs = {key: value for key, value in data.items() if key in inspect.signature(function).parameters}
    if route == "customer": kwargs["customer_id"] = p.order.customer_id
    response = asyncio.run(function(request=Req(), db=p.env.db, **kwargs))
    assert response.status_code == 200 and len(p.sent) == 1
    assert f"/finance/orders/{p.order.id}?" in json.loads(response.body)["redirect_url"]
    assert p.sent[0]["subject"] == data["subject"]
    assert "My edited order" in p.sent[0]["html_content"]
    assert p.sent[0]["recipient_email"] == data["recipient_email"]
    assert [file.content for file in p.sent[0]["attachments"]] == [b"%PDF-order-test"]
    email = p.env.db.scalar(select(CustomerZohoEmail))
    assert email.customer_id == p.order.customer_id and email.sync_status == "sent"
    view = mailbox_for(p.env.service).communications.get_view(customer_id=p.order.customer_id, actor=p.env.user.username)
    assert any(item.id == email.id for item in view.emails)
    assert p.env.db.get(HubMailboxEmail, p.context["draft_id"]) is None
    assert p.order.encrypted_fields_json == before
    with pytest.raises(ValueError):
        p.env.service.execute("finance.orders.email.send", data)
    assert len(p.sent) == 1


@pytest.mark.parametrize("change", ["attachment", "order", "pdf", "schedule", "customer", "empty", "permission"])
def test_rejects_invalid_draft_before_smtp(prepared, monkeypatch, change):
    p = prepared
    data = dict(p.data)
    service = p.env.service
    if change == "attachment": data["retained_attachment_ids"] = "[]"
    if change == "order": p.order.encrypted_fields_json = p.env.cipher.encrypt(json.dumps({"status": "changed"}))
    if change == "pdf": monkeypatch.setattr("app.services.finance_invoice_pdf_storage.FinanceInvoicePdfStorage.load", lambda *_: b"changed")
    if change == "schedule": data["scheduled_at"] = "2026-10-01T10:00"
    if change == "customer": data["recipient_customer_id"] = str(p.env.hidden.id)
    if change == "empty": data["content"] = ""
    if change == "permission": service = HubOperationService(db=p.env.db, cipher=p.env.cipher, actor=p.env.sales.username)
    with pytest.raises(ValueError): service.execute("finance.orders.email.send", data)
    assert not p.sent


def test_saved_draft_preserves_order_customer_and_attachment(prepared):
    p = prepared
    p.env.service.execute("emails.drafts.save", {**p.data, "content": "<p>Saved</p>", "recipient_key": "",
        "recipient_email": "another@example.test", "recipient_customer_id": "", "context_module": "", "context_record_id": ""})
    p.env.db.commit()
    context = mailbox_for(p.env.service).get_draft_compose_context(draft_id=p.context["draft_id"])
    assert context["customer_id"] == p.order.customer_id
    assert context["context_module"] == "orders" and context["order_id"] == p.order.id
    assert len(context["attachments"]) == 1 and not p.sent
    with pytest.raises(ValueError, match="anderen Kunden"):
        p.env.service.execute("emails.drafts.save", {**p.data, "recipient_customer_id": str(p.env.hidden.id),
            "context_module": "", "context_record_id": ""})


def test_generated_pdf_preferred_and_pending_generation_not_bypassed(prepared, monkeypatch):
    p = prepared
    pdf = HubFinanceGeneratedPdf(document_type="orders", document_id=p.order.id, status="ready",
        template_name="Order", template_version=1, generation_token="order-generation", filename="generated.pdf",
        storage_key="b" * 48, byte_size=20, requested_at=datetime.now(UTC))
    p.env.db.add(pdf)
    p.env.db.commit()
    monkeypatch.setattr("app.services.finance_generated_pdf_storage.FinanceGeneratedPdfStorage.load", lambda *_: b"%PDF-generated")
    result = p.env.service.execute("finance.orders.email.prepare", {"record_id": str(p.order.id)})
    context = mailbox_for(p.env.service).get_draft_compose_context(draft_id=result.record_id)
    assert context["attachments"][0]["filename"] == "generated.pdf"
    attachments = mailbox_for(p.env.service).prepare_draft_delivery_attachments(draft_id=result.record_id, retained_attachment_ids=None)
    assert attachments[0].content == b"%PDF-generated"
    pdf.status = "queued"
    p.env.db.commit()
    with pytest.raises(ValueError): p.env.service.execute("finance.orders.email.prepare", {"record_id": str(p.order.id)})
    assert not p.sent


def test_prepare_and_send_respect_revoked_customer_scope(prepared, monkeypatch):
    from app.services.hub_access_control import HubAccessControlService
    p = prepared
    monkeypatch.setattr(HubAccessControlService, "can_access_record", lambda *args, **kwargs: False)
    for operation, data in (("prepare", {"record_id": str(p.order.id)}), ("send", p.data)):
        with pytest.raises(ValueError): p.env.service.execute(f"finance.orders.email.{operation}", data)
    assert not p.sent


def test_converted_lead_offer_requires_matching_customer_conversion(prepared):
    from app.models.hub_finance_offer import HubFinanceOffer
    from app.models.hub_lead import HubLead
    from app.models.hub_lead_conversion import HubLeadConversion
    p = prepared
    lead = HubLead(encrypted_profile_json=p.env.cipher.encrypt(json.dumps({"fields": {"company": "Customer before conversion"}})))
    p.env.db.add(lead)
    p.env.db.flush()
    offer = HubFinanceOffer(lead_id=lead.id, encrypted_fields_json=p.env.cipher.encrypt('{}'))
    p.env.db.add(offer)
    p.env.db.flush()
    p.order.offer_id = offer.id
    p.env.db.commit()
    with pytest.raises(ValueError, match="anderen Kunden"):
        p.env.service.execute("finance.orders.email.prepare", {"record_id": str(p.order.id)})
    conversion = HubLeadConversion(lead_id=lead.id, customer_id=p.order.customer_id,
        converted_at=datetime.now(UTC), actor_name="Admin", actor_origin="hub")
    p.env.db.add(conversion)
    p.env.db.commit()
    result = p.env.service.execute("finance.orders.email.prepare", {"record_id": str(p.order.id)})
    assert mailbox_for(p.env.service).get_draft_compose_context(draft_id=result.record_id)["customer_id"] == p.order.customer_id
    conversion.customer_id = p.env.hidden.id
    p.env.db.commit()
    with pytest.raises(ValueError, match="anderen Kunden"):
        p.env.service.execute("finance.orders.email.prepare", {"record_id": str(p.order.id)})
    assert not p.sent


def test_known_failure_retry_and_unknown_outcome_guard(prepared, monkeypatch):
    p = prepared
    monkeypatch.setattr(CustomerCommunicationService, "send_email", lambda *args, **kwargs: SimpleNamespace(success=False))
    with pytest.raises(ValueError, match="fehlgeschlagen"):
        p.env.service.execute("finance.orders.email.send", p.data)
    def fail(*args, **kwargs):
        with pytest.raises(ValueError, match="läuft"):
            p.env.service.execute("finance.orders.email.send", p.data)
        raise RuntimeError("Lost SMTP response")
    monkeypatch.setattr(CustomerCommunicationService, "send_email", fail)
    with pytest.raises(ValueError, match="unklar"):
        p.env.service.execute("finance.orders.email.send", p.data)
    with pytest.raises(ValueError, match="unklar"):
        p.env.service.execute("finance.orders.email.send", p.data)
    with pytest.raises(ValueError, match="unklar"):
        p.env.service.execute("emails.drafts.save", p.data)
    assert p.env.db.get(HubMailboxEmail, p.context["draft_id"]) is not None


@pytest.mark.parametrize("missing", ["pdf", "contact_email", "customer", "template"])
def test_prepare_missing_requirements(prepared, missing):
    p = prepared
    count = p.env.db.query(HubMailboxEmail).count()
    if missing == "pdf": p.env.db.delete(p.env.db.scalar(select(HubFinanceOrderPdf)))
    if missing == "customer": p.order.customer_id = None
    if missing == "contact_email": p.env.contact.encrypted_profile_json = p.env.cipher.encrypt('{}')
    if missing == "template": p.env.db.delete(p.env.db.scalar(select(ZohoEmailTemplate)))
    p.env.db.commit()
    with pytest.raises(ValueError): p.env.service.execute("finance.orders.email.prepare", {"record_id": str(p.order.id)})
    assert p.env.db.query(HubMailboxEmail).count() == count and not p.sent


def test_button_endpoint_csrf_and_browser_fixture(prepared, monkeypatch):
    p = prepared
    app = create_app()
    request = Request({"type": "http", "method": "GET", "path": f"/finance/orders/{p.order.id}", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("hub.test", 80), "session": {}, "app": app, "router": app.router})
    request.state.hub_user = p.env.user
    page = web.finance_document_detail_page("orders", p.order.id, request, p.env.db).body.decode()
    assert f'data-order-email-compose="{p.order.id}"' in page
    assert page.index('data-customer-edit-open') < page.index('data-order-email-compose')
    assert permission_target(f"/finance/orders/{p.order.id}/email-compose", "POST") == ("emails", "create")
    context = web.prepare_finance_order_email(p.order.id, request, p.env.db)
    assert context["order_id"] == p.order.id and not p.sent
    def deny(*args): raise HTTPException(403)
    monkeypatch.setattr(web, "require_csrf", deny)
    with pytest.raises(HTTPException): web.prepare_finance_order_email(p.order.id, request, p.env.db)
    directory = os.environ.get("HUB_ORDER_COMPOSER_ARTIFACT_DIR")
    if directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root / "order.html").write_text(page, encoding="utf-8")
        (root / "data.json").write_text(json.dumps({"order": p.order.id, "context": p.context}), encoding="utf-8")
