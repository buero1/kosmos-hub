import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_documents import HubFinanceInvoice
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.zoho_email_template import ZohoEmailTemplate
from app.services.hub_invoice_email_batches import (
    HubInvoiceEmailBatchService, InvoiceEmailBatchError, run_invoice_email_batch,
    resume_queued_invoice_email_batches,
)


def _fixture(db, *, contact_email="kunde@example.de", pdf=True, template_subject="Rechnung ${Invoice.Number}"):
    cipher = SecretCipher("a" * 32)
    customer = Customer(name="Muster GmbH", encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {}})))
    db.add(customer)
    db.flush()
    contact = CustomerContact(customer_id=customer.id, encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {
        "Name": "Max Muster", "E-Mail": contact_email, "Vorname": "Max", "Nachname": "Muster",
    }})))
    db.add(contact)
    db.flush()
    invoice = HubFinanceInvoice(
        invoice_number="RE-000042", customer_id=customer.id, contact_id=contact.id,
        encrypted_fields_json=cipher.encrypt(json.dumps({
            "status": "draft", "invoice_date": "2026-09-13", "due_date": "2026-09-27", "currency": "EUR",
        })),
    )
    db.add(invoice)
    db.flush()
    if pdf:
        db.add(HubFinanceInvoicePdf(
            invoice_id=invoice.id, filename="RE-000042.pdf", storage_key="a" * 48,
            byte_size=12, content_type="application/pdf", is_zugferd=True,
            imported_at=datetime.now(UTC),
        ))
    db.add(ZohoEmailTemplate(
        zoho_template_id="hub-template-invoice-test", module="Accounts", is_active=True,
        encrypted_payload_json=cipher.encrypt(json.dumps({
            "name": "Rechnungen senden", "subject": template_subject,
            "content": "<p>Guten Tag ${Contact.Name}, Fälligkeit ${Invoice.DueDate}</p>",
        })), zoho_synced_at=datetime.now(UTC),
    ))
    db.add(HubMailboxAccount(
        email_address="info@kosmos-medien.de", display_name="Kosmos", username="info@kosmos-medien.de",
        encrypted_password=cipher.encrypt("not-used"), verified_at=datetime.now(UTC),
    ))
    db.commit()
    return cipher, invoice.id, contact


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def test_review_lists_contact_and_email_then_requires_fresh_confirmation():
    engine = _db()
    with Session(engine) as db:
        cipher, invoice_id, contact = _fixture(db)
        service = HubInvoiceEmailBatchService(db=db, cipher=cipher)
        review = service.review([invoice_id], actor="admin")
        assert review["rows"][0]["email"] == "kunde@example.de"
        assert review["rows"][0]["contact"] == "Max Muster"
        assert review["sender"] == "info@kosmos-medien.de"
        assert review["review_token"]
        with pytest.raises(InvoiceEmailBatchError, match="ungültig"):
            service.confirm(token=review["review_token"], actor="other")
        batch = service.confirm(token=review["review_token"], actor="admin")
        assert service.confirm(token=review["review_token"], actor="admin").id == batch.id
        assert service.status(batch_id=batch.id, actor="admin")["items"][0]["status"] == "queued"

        fresh_review = service.review([invoice_id], actor="admin")
        contact.encrypted_profile_json = cipher.encrypt(json.dumps({"fields": {"Name": "Max Muster", "E-Mail": "neu@example.de"}}))
        db.flush()
        with pytest.raises(InvoiceEmailBatchError, match="geändert"):
            service.confirm(token=fresh_review["review_token"], actor="admin")


@pytest.mark.parametrize("contact_email,pdf,template_subject,expected", [
    ("", True, "Rechnung ${Invoice.Number}", "Keine E-Mail-Adresse"),
    ("kunde@example.de", False, "Rechnung ${Invoice.Number}", "Keine fertige ZUGFeRD-PDF"),
    ("kunde@example.de", True, "Rechnung ${Invoice.Unknown}", "Unbekannte Platzhalter"),
])
def test_review_blocks_missing_prerequisites(contact_email, pdf, template_subject, expected):
    engine = _db()
    with Session(engine) as db:
        cipher, invoice_id, _contact = _fixture(db, contact_email=contact_email, pdf=pdf, template_subject=template_subject)
        review = HubInvoiceEmailBatchService(db=db, cipher=cipher).review([invoice_id], actor="admin")
        assert expected in review["rows"][0]["issue"]
        assert review["review_token"] == ""


def test_worker_sends_once_only_after_confirmation(monkeypatch):
    engine = _db()
    with Session(engine) as db:
        cipher, invoice_id, _contact = _fixture(db)
        service = HubInvoiceEmailBatchService(db=db, cipher=cipher)
        batch = service.confirm(token=service.review([invoice_id], actor="admin")["review_token"], actor="admin")
        batch_id = batch.id
        db.commit()

    monkeypatch.setattr("app.services.hub_invoice_email_batches.SessionLocal", lambda: Session(engine))
    monkeypatch.setattr("app.services.hub_invoice_email_batches.get_secret_cipher", lambda: cipher)
    monkeypatch.setattr("app.services.hub_invoice_email_batches.FinanceInvoicePdfStorage.load", lambda *_: b"%PDF-fake")
    sent = []

    def fake_send(_self, **kwargs):
        sent.append((kwargs["sender_email"], kwargs["recipient_key"], kwargs["subject"], kwargs["attachments"][0].content))
        return type("Result", (), {"success": True, "message": "Gesendet"})()

    monkeypatch.setattr("app.services.hub_invoice_email_batches.CustomerCommunicationService.send_email", fake_send)
    run_invoice_email_batch(batch_id)
    run_invoice_email_batch(batch_id)
    assert len(sent) == 1
    assert sent[0][0] == "info@kosmos-medien.de"
    assert sent[0][1].startswith("contact:")
    assert sent[0][2] == "Rechnung RE-000042"
    with Session(engine) as db:
        assert HubInvoiceEmailBatchService(db=db, cipher=cipher).status(batch_id=batch_id, actor="admin")["items"][0]["status"] == "sent"


def test_restart_does_not_retry_an_uncertain_delivery(monkeypatch):
    engine = _db()
    with Session(engine) as db:
        cipher, invoice_id, _contact = _fixture(db)
        service = HubInvoiceEmailBatchService(db=db, cipher=cipher)
        batch = service.confirm(token=service.review([invoice_id], actor="admin")["review_token"], actor="admin")
        batch_id = batch.id
        batch.status = "running"
        batch.items[0].status = "sending"
        db.commit()

    monkeypatch.setattr("app.services.hub_invoice_email_batches.SessionLocal", lambda: Session(engine))
    resume_queued_invoice_email_batches()
    with Session(engine) as db:
        status = HubInvoiceEmailBatchService(db=db, cipher=cipher).status(batch_id=batch_id, actor="admin")
        assert status["status"] == "interrupted"
        assert status["items"][0]["status"] == "uncertain"


def test_batch_rejects_duplicate_and_overlarge_selection():
    with pytest.raises(InvoiceEmailBatchError, match="ungültig"):
        HubInvoiceEmailBatchService._ids([2, 2])
    with pytest.raises(InvoiceEmailBatchError, match="1 bis 1.000"):
        HubInvoiceEmailBatchService._ids(list(range(1, 1002)))
