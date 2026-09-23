from io import BytesIO
import json
from datetime import UTC, datetime, timedelta

from facturx import get_facturx_xml_from_pdf
from pypdf import PdfWriter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_lead import HubLead
from app.services.email_compose_images import EmailComposeImageService
from app.services.finance_generated_pdf_storage import FinanceGeneratedPdfStorage
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import DUNNING_MODULE, INVOICE_MODULE, HubFinanceDocumentService
from app.services.hub_finance_pdf_generation import HubFinancePdfError, HubFinancePdfService


def _blank_pdf() -> bytes:
    target = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(target)
    return target.getvalue()


def _invoice_values() -> dict[str, str]:
    return {
        "document_field__status": "draft",
        "document_field__invoice_date": "2026-09-12",
        "document_field__due_date": "2026-09-26",
        "document_field__currency": "EUR",
        "document_field__payment_terms": "14_days",
        "document_line__0__name": "Website-Paket",
        "document_line__0__sku": "WEB-1",
        "document_line__0__description": "Konzeption und Umsetzung",
        "document_line__0__quantity": "2",
        "document_line__0__unit": "Einmalig",
        "document_line__0__unit_price": "100",
        "document_line__0__discount_percent": "10",
        "document_line__0__tax_rate": "19",
    }


def _offer_values() -> dict[str, str]:
    return {
        "offer_field__status": "draft",
        "offer_field__offer_date": "2026-09-17",
        "offer_field__valid_until": "2026-10-17",
        "offer_field__currency": "EUR",
        "offer_line__0__name": "Website-Paket",
        "offer_line__0__description": "Konzeption und Umsetzung",
        "offer_line__0__quantity": "1",
        "offer_line__0__unit": "Einmalig",
        "offer_line__0__unit_price": "100",
        "offer_line__0__discount_percent": "0",
        "offer_line__0__tax_rate": "19",
    }


def test_offer_pdf_snapshot_uses_lead_recipient_and_address_fields():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        lead = HubLead(
            encrypted_profile_json=cipher.encrypt(json.dumps({
                "schema_version": 1,
                "source": "hub",
                "fields": {
                    "salutation": "Frau",
                    "first_name": "Lena",
                    "last_name": "Leitner",
                    "company": "Leitner Design",
                    "email": "lena@example.com",
                    "street": "Musterweg 7",
                    "postal_code": "80331",
                    "city": "München",
                    "country": "Deutschland",
                },
                "subforms": {},
            }))
        )
        db.add(lead)
        db.flush()
        offer = HubFinanceService(db=db, cipher=cipher).create_offer(
            customer_id=None,
            contact_id=None,
            lead_id=lead.id,
            submitted_values=_offer_values(),
        )

        snapshot = HubFinancePdfService(db=db, cipher=cipher)._snapshot(
            document_type="offers",
            document_id=offer.id,
        )

        assert snapshot.customer_name == "Leitner Design"
        assert snapshot.contact_name == "Lena Leitner"
        assert snapshot.billing_street == "Musterweg 7"
        assert snapshot.billing_postal_code == "80331"
        assert snapshot.billing_city == "München"
        assert snapshot.contact_fields["email"] == "lena@example.com"


def test_invoice_generation_uses_default_revision_and_embeds_xsd_valid_zugferd(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    stored: dict[str, bytes] = {}
    embedded_html: list[str] = []

    monkeypatch.setattr(
        HubFinancePdfService,
        "_render_pdf",
        staticmethod(lambda *, html, snapshot: _blank_pdf()),
    )
    monkeypatch.setattr(
        EmailComposeImageService,
        "embed_local_images",
        lambda self, content: embedded_html.append(content) or content,
    )
    monkeypatch.setattr(
        FinanceGeneratedPdfStorage,
        "store",
        lambda self, content: stored.setdefault("generated", content) and "generated",
    )
    monkeypatch.setattr(FinanceGeneratedPdfStorage, "load", lambda self, storage_key: stored[storage_key])
    monkeypatch.setattr(FinanceGeneratedPdfStorage, "remove", lambda self, storage_key: stored.pop(storage_key, None))

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        contact = CustomerContact(
            customer=customer,
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Name": "Test Kontakt"}})),
        )
        db.add(contact)
        db.flush()
        document = HubFinanceDocumentService(db=db, cipher=cipher).create_document(
            module=INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=contact.id,
            link_id=None,
            submitted_values=_invoice_values(),
        )
        pdf_service = HubFinancePdfService(db=db, cipher=cipher)
        token = pdf_service.queue(document_type="invoices", document_id=document.id)
        db.commit()

        assert document.pdf_template_id is not None
        pdf_service.generate(generation_token=token)

        generated = db.scalar(select(HubFinanceGeneratedPdf))
        assert generated is not None
        assert generated.status == "ready"
        assert generated.is_zugferd
        assert generated.zugferd_version == "2.5.2"
        assert generated.zugferd_profile == "EN 16931"
        assert generated.validation_status == "xsd-valid-draft"
        assert generated.template_name == "Standardrechnung"
        assert generated.template_version == 1
        assert len(embedded_html) == 1

        _, embedded_xml = get_facturx_xml_from_pdf(stored["generated"])
        assert document.invoice_number.encode() in embedded_xml
        assert b"Website-Paket" in embedded_xml


def test_generated_pdf_html_replaces_document_values_without_preview_samples():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(
            name="Echter Kunde GmbH",
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Website": "https://kunde.example"}})),
        )
        db.add(customer)
        db.flush()
        contact = CustomerContact(
            customer=customer,
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {
                "Name": "Test Kontakt", "Anrede": "Frau", "Vorname": "Test", "Nachname": "Kontakt",
                "Briefanrede": "Sehr geehrte Frau",
            }})),
        )
        db.add(contact)
        db.flush()
        document = HubFinanceDocumentService(db=db, cipher=cipher).create_document(
            module=INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=contact.id,
            link_id=None,
            submitted_values=_invoice_values(),
        )
        service = HubFinancePdfService(db=db, cipher=cipher)
        token = service.queue(document_type="invoices", document_id=document.id)
        generated = db.scalar(
            select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.generation_token == token)
        )
        snapshot = service._snapshot(document_type="invoices", document_id=document.id)
        from app.models.hub_pdf_template import HubPdfTemplateRevision
        from app.services.hub_pdf_templates import HubPdfTemplateService

        revision = db.get(HubPdfTemplateRevision, generated.template_revision_id)
        content = HubPdfTemplateService(db=db).decoded_content(
            document_type="invoices",
            content_json=revision.content_json,
        )
        content["blocks"]["intro"]["content_html"] = (
            "<p>${Contact.Greeting}, ${Contact.FirstName}. ${Customer.Website} ${Invoice.DueDate}</p>"
        )
        html = service._render_html(snapshot=snapshot, content=content)

        assert "Echter Kunde GmbH" in html
        assert document.invoice_number in html
        assert "Website-Paket" in html
        assert "Schreinerei Muster" not in html
        assert "Sehr geehrte Frau Kontakt" in html
        assert "https://kunde.example" in html
        assert "26.09.2026" in html
        assert "Allgemeine Geschäftsbedingungen" not in html
        assert "${" not in html


def test_dunning_generation_uses_its_template_and_references_the_source_invoice(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    rendered_html: list[str] = []
    stored: dict[str, bytes] = {}

    monkeypatch.setattr(
        HubFinancePdfService,
        "_render_pdf",
        staticmethod(lambda *, html, snapshot: rendered_html.append(html) or _blank_pdf()),
    )
    monkeypatch.setattr(FinanceGeneratedPdfStorage, "store", lambda self, content: stored.setdefault("generated", content) and "generated")
    monkeypatch.setattr(FinanceGeneratedPdfStorage, "load", lambda self, storage_key: stored[storage_key])

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        contact = CustomerContact(
            customer=customer,
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Name": "Test Kontakt"}})),
        )
        db.add(contact)
        db.flush()
        document_service = HubFinanceDocumentService(db=db, cipher=cipher)
        invoice = document_service.create_document(
            module=INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=contact.id,
            link_id=None,
            submitted_values=_invoice_values(),
        )
        draft = document_service.dunning_draft_from_invoice(invoice_id=invoice.id)
        dunning = document_service.create_document(
            module=DUNNING_MODULE,
            customer_id=draft.customer_id,
            contact_id=draft.contact_id,
            link_id=draft.invoice_id,
            submitted_values=draft.submitted_values,
        )
        pdf_service = HubFinancePdfService(db=db, cipher=cipher)
        token = pdf_service.queue(document_type="dunnings", document_id=dunning.id)
        db.commit()

        pdf_service.generate(generation_token=token)
        generated = db.scalar(
            select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.generation_token == token)
        )

        assert generated is not None
        assert generated.status == "ready"
        assert not generated.is_zugferd
        assert generated.template_name == "Standardmahnung"
        assert invoice.invoice_number in rendered_html[0]
        assert "Website-Paket" in rendered_html[0]
        assert "${" not in rendered_html[0]


def test_failed_pdf_generation_retries_twice_without_new_document():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = HubFinanceGeneratedPdf(
            document_type="invoices",
            document_id=1,
            template_name="Fehlende Vorlage",
            template_version=1,
            template_id=999,
            generation_token="retry-test",
            requested_at=datetime.now(UTC),
            status="queued",
        )
        db.add(job)
        db.commit()
        service = HubFinancePdfService(db=db, cipher=SecretCipher("a" * 32))
        for attempt in (1, 2, 3):
            try:
                service.generate(generation_token="retry-test")
            except HubFinancePdfError as exc:
                db.rollback()
                service.mark_failed(generation_token="retry-test", error=exc)
            assert job.attempt_count == attempt
            assert job.status == ("failed" if attempt == 3 else "queued")
            if attempt < 3:
                assert job.next_retry_at is not None
                job.next_retry_at = datetime.now(UTC) - timedelta(minutes=1)
                db.commit()
        assert job.next_retry_at is None
