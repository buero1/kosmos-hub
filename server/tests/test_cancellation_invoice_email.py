import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.models.hub_finance_documents import HubFinanceCancellationInvoice, HubFinanceInvoice
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_email_composition import mailbox_for
from app.services.hub_finance_documents import CANCELLATION_INVOICE_MODULE, HubFinanceDocumentService
from app.services.hub_operation_invoice_email import SEND_FIELDS
from app.services.hub_operations import agent_operations
from test_hub_finance_operations import env


@pytest.fixture
def prepared_cancellation(env, monkeypatch):
    env.contact.encrypted_profile_json = env.cipher.encrypt(json.dumps({
        "fields": {"Name": "Test contact", "E-Mail": "test@example.test"},
    }))
    invoice = HubFinanceInvoice(
        customer_id=env.customer.id,
        contact_id=env.contact.id,
        invoice_number="RE-TEST",
        encrypted_fields_json=env.cipher.encrypt(json.dumps({
            "status": "cancelled",
            "invoice_date": "2026-09-25",
            "currency": "EUR",
        })),
    )
    env.db.add(invoice)
    env.db.flush()
    cancellation = HubFinanceCancellationInvoice(
        customer_id=env.customer.id,
        contact_id=env.contact.id,
        invoice_id=invoice.id,
        cancellation_number="ST-TEST",
        encrypted_fields_json=env.cipher.encrypt(json.dumps({
            "status": "created",
            "cancellation_date": "2026-10-03",
            "cancellation_reason": "Rechnung vollständig storniert.",
            "source_invoice_date": "2026-09-25",
            "currency": "EUR",
        })),
    )
    env.db.add(cancellation)
    env.db.flush()
    env.db.add(HubFinanceGeneratedPdf(
        document_type="cancellation-invoices",
        document_id=cancellation.id,
        template_name="Standardstornorechnung",
        template_version=1,
        status="ready",
        generation_token="cancellation-test-token",
        filename="ST-TEST.pdf",
        byte_size=22,
        storage_key="cancellation-test-pdf",
        is_zugferd=True,
        zugferd_version="2.5.2",
        zugferd_profile="EN 16931",
        validation_status="xsd-valid-draft",
        requested_at=datetime.now(UTC),
        generated_at=datetime.now(UTC),
    ))
    env.db.commit()
    pdf_content = b"%PDF-cancellation-test"
    monkeypatch.setattr(
        "app.services.finance_generated_pdf_storage.FinanceGeneratedPdfStorage.load",
        lambda *_: pdf_content,
    )
    sent = []

    def send(_self, **kwargs):
        sent.append(kwargs)
        return SimpleNamespace(success=True, email_id=123)

    monkeypatch.setattr(CustomerCommunicationService, "send_email", send)
    result = env.service.execute(
        "finance.cancellation-invoices.email.prepare",
        {"record_id": str(cancellation.id)},
    )
    env.db.commit()
    context = mailbox_for(env.service).get_draft_compose_context(draft_id=result.record_id)
    data = {field: str(context.get(field) or "") for field in SEND_FIELDS}
    data.update({
        "recipient_customer_id": str(cancellation.customer_id),
        "recipient_key": context["recipient"]["key"],
        "subject": "Stornorechnung ST-TEST",
        "content": "<p>Anbei erhalten Sie die Stornorechnung.</p>",
        "retained_attachment_ids": json.dumps([item["id"] for item in context["attachments"]]),
    })
    return SimpleNamespace(
        env=env,
        invoice=invoice,
        cancellation=cancellation,
        context=context,
        data=data,
        sent=sent,
        pdf_content=pdf_content,
    )


def test_prepare_and_send_cancellation_invoice_exactly_once(prepared_cancellation):
    prepared = prepared_cancellation
    assert prepared.context["attachments"][0]["filename"] == "ST-TEST.pdf"
    assert prepared.context["cancellation_invoice_id"] == prepared.cancellation.id
    assert "finance.cancellation-invoices.email.send" not in {
        operation.key for operation in agent_operations()
    }

    result = prepared.env.service.execute(
        "finance.cancellation-invoices.email.send",
        prepared.data,
    )

    assert result.href.startswith(
        f"/finance/cancellation-invoices/{prepared.cancellation.id}?"
    )
    assert len(prepared.sent) == 1
    assert prepared.sent[0]["subject"] == prepared.data["subject"]
    assert prepared.sent[0]["attachments"][0].content == prepared.pdf_content
    values = HubFinanceDocumentService(
        db=prepared.env.db,
        cipher=prepared.env.cipher,
    )._document_values(
        module=CANCELLATION_INVOICE_MODULE,
        document=prepared.cancellation,
    )
    assert values["status"] == "sent"
    assert prepared.env.db.get(HubMailboxEmail, prepared.context["draft_id"]) is None
    assert prepared.env.db.scalar(
        select(HubFinanceCancellationInvoice).where(
            HubFinanceCancellationInvoice.id == prepared.cancellation.id
        )
    ).invoice_id == prepared.invoice.id

    with pytest.raises(ValueError):
        prepared.env.service.execute(
            "finance.cancellation-invoices.email.send",
            prepared.data,
        )
    assert len(prepared.sent) == 1


def test_invoice_and_cancellation_pages_show_the_controlled_workflow(prepared_cancellation):
    prepared = prepared_cancellation
    app = create_app()

    def request(path):
        result = Request({
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("hub.test", 80),
            "session": {},
            "app": app,
            "router": app.router,
        })
        result.state.hub_user = prepared.env.user
        return result

    invoice_page = web.finance_document_detail_page(
        "invoices",
        prepared.invoice.id,
        request(f"/finance/invoices/{prepared.invoice.id}"),
        prepared.env.db,
    ).body.decode()
    cancellation_page = web.finance_document_detail_page(
        "cancellation-invoices",
        prepared.cancellation.id,
        request(f"/finance/cancellation-invoices/{prepared.cancellation.id}"),
        prepared.env.db,
    ).body.decode()

    assert f'href="/finance/cancellation-invoices/{prepared.cancellation.id}"' in invoice_page
    assert "Stornorechnung öffnen" in invoice_page
    assert "Rechnung stornieren" not in invoice_page
    assert not re.search(r"<button[^>]+data-customer-edit-open", invoice_page)
    assert f'data-cancellation-email-compose="{prepared.cancellation.id}"' in cancellation_page
    assert f'href="/finance/invoices/{prepared.invoice.id}"' in cancellation_page
    assert "ZUGFeRD-Stornorechnung" in cancellation_page
    assert not re.search(r"<button[^>]+data-customer-edit-open", cancellation_page)


@pytest.mark.parametrize("change", ["document", "pdf", "schedule", "attachment"])
def test_reject_changed_cancellation_before_sending(prepared_cancellation, change):
    prepared = prepared_cancellation
    data = dict(prepared.data)
    if change == "document":
        prepared.cancellation.encrypted_fields_json = prepared.env.cipher.encrypt(json.dumps({
            "status": "created",
            "cancellation_date": "2026-10-04",
            "cancellation_reason": "Geändert",
            "source_invoice_date": "2026-09-25",
            "currency": "EUR",
        }))
    elif change == "pdf":
        prepared.env.db.scalar(
            select(HubFinanceGeneratedPdf).where(
                HubFinanceGeneratedPdf.document_type == "cancellation-invoices"
            )
        ).storage_key = "changed-cancellation-pdf"
    elif change == "schedule":
        data["scheduled_at"] = "2026-10-04T11:00"
    else:
        data["retained_attachment_ids"] = "[]"

    with pytest.raises(ValueError):
        prepared.env.service.execute(
            "finance.cancellation-invoices.email.send",
            data,
        )
    assert prepared.sent == []
