import json
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_documents import HubFinanceRecurringInvoice
from app.services.hub_finance_documents import RECURRING_INVOICE_MODULE, HubFinanceDocumentService
from app.services.zoho_books import ZohoBooksError
from app.services.zoho_books_recurring_invoice_import import ZohoBooksRecurringInvoiceImportService


class FakeBooksRecurringInvoiceReader:
    def __init__(self):
        self.invoice = {
            "recurring_invoice_id": "7001",
            "recurrence_name": "Website-Betreuung Beispiel GmbH",
            "status": "active",
            "customer_name": "Beispiel GmbH",
            "customer_id": "books-customer-1",
            "contact_persons_associated": [{
                "contact_person_id": "books-contact-1",
                "contact_person_email": "person@example.test",
            }],
            "start_date": "2026-01-01",
            "end_date": "",
            "next_invoice_date": "2026-12-01",
            "recurrence_frequency": "months",
            "repeat_every": 6,
            "recurrence_preferences": "save_as_draft",
            "currency_code": "EUR",
            "payment_terms": 7,
            "last_modified_time": "2026-09-11T10:15:00+0200",
            "custom_field_hash": {
                "cf_mahngeb_unformatted": 9,
                "cf_systemzahlung_erledigt_unformatted": True,
            },
            "line_items": [{
                "item_id": "books-item-1",
                "name": "Jahresbeitrag Homepage",
                "description": "Wartung und Betreuung",
                "quantity": 1,
                "unit": "Halbjährlich",
                "rate": "49.99",
                "discount": "5%",
                "tax_percentage": 19,
            }, {
                "name": "Freitextposition",
                "description": "Zusätzlicher Speicherplatz",
                "quantity": 2,
                "rate": "10",
                "tax_percentage": 19,
            }],
        }

    @staticmethod
    def get_status():
        return SimpleNamespace(organization_id="books-org-1")

    @staticmethod
    def list_all_recurring_invoice_ids():
        return ("7001",)

    def get_recurring_invoice(self, *, recurring_invoice_id: str):
        assert recurring_invoice_id == "7001"
        return self.invoice

    @staticmethod
    def get_books_contact(*, contact_id: str):
        assert contact_id == "books-customer-1"
        return {
            "zcrm_account_id": "crm-customer-1",
            "contact_persons": [{
                "contact_person_id": "books-contact-1",
                "zcrm_contact_id": "crm-contact-1",
                "email": "person@example.test",
            }],
        }


class FailingBooksRecurringInvoiceReader:
    @staticmethod
    def get_status():
        return SimpleNamespace(organization_id="books-org-1")

    @staticmethod
    def list_all_recurring_invoice_ids():
        return ("failure-1", "failure-2", "failure-3", "failure-4")

    @staticmethod
    def get_recurring_invoice(*, recurring_invoice_id: str):
        raise ZohoBooksError(f"Books test failure for {recurring_invoice_id}")

    @staticmethod
    def get_books_contact(*, contact_id: str):
        raise AssertionError("No Books contact should be requested after a failed invoice request.")


def test_recurring_invoice_full_import_is_idempotent_and_keeps_schedule_and_lines():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(name="Beispiel GmbH", zoho_id="crm-customer-1")
        contact = CustomerContact(
            customer=customer,
            zoho_id="crm-contact-1",
            encrypted_profile_json=cipher.encrypt(json.dumps({
                "fields": {"Name": "Erika Beispiel", "E-Mail": "person@example.test"}
            })),
        )
        article = HubFinanceArticle(
            zoho_books_id="books-item-1",
            encrypted_fields_json=cipher.encrypt(json.dumps({"name": "Jahresbeitrag Homepage", "sku": "ART-1"})),
        )
        db.add_all((customer, contact, article))
        db.commit()
        service = ZohoBooksRecurringInvoiceImportService(
            db=db,
            cipher=cipher,
            books_service=FakeBooksRecurringInvoiceReader(),
        )

        status, started = service.start_all(requested_by="books-admin")
        db.commit()
        assert started is True
        assert status.total_invoices == 1
        assert service.process_next_invoice() == "succeeded"
        assert service.process_next_invoice() == "completed"

        invoice = db.scalar(
            select(HubFinanceRecurringInvoice).where(HubFinanceRecurringInvoice.zoho_books_id == "7001")
        )
        assert invoice is not None
        assert invoice.customer_id == customer.id
        assert invoice.contact_id == contact.id
        assert invoice.lines[0].article_id == article.id
        values = json.loads(cipher.decrypt(invoice.encrypted_fields_json))
        assert values["interval_unit"] == "quarter"
        assert values["interval_count"] == "2"
        assert values["automatic_creation"] == "false"
        assert values["payment_terms"] == "7_days"
        assert values["late_fee"] == "9.00"
        assert values["system_payment_complete"] == "true"

        detail = HubFinanceDocumentService(db=db, cipher=cipher).get_detail(
            module=RECURRING_INVOICE_MODULE,
            document_id=invoice.id,
        )
        assert detail is not None
        assert detail.lines[0].discount_percent == "5.00"
        assert detail.lines[1].name == "Zusätzlicher Speicherplatz"
        assert detail.lines[1].description == ""

        second_status, second_started = service.start_all(requested_by="books-admin")
        db.commit()
        assert second_started is True
        assert second_status.total_invoices == 1
        assert service.process_next_invoice() == "succeeded"
        assert service.process_next_invoice() == "completed"
        assert db.scalars(
            select(HubFinanceRecurringInvoice).where(HubFinanceRecurringInvoice.zoho_books_id == "7001")
        ).all() == [invoice]
        assert service.status().updated_invoices == 1


def test_recurring_invoice_import_can_be_cancelled():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = ZohoBooksRecurringInvoiceImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            books_service=FakeBooksRecurringInvoiceReader(),
        )
        service.start_all(requested_by="books-admin")
        db.commit()

        status, requested = service.cancel()
        db.commit()
        assert requested is True
        assert status is not None and status.cancel_requested is True
        assert service.process_next_invoice() == "cancelled"
        assert service.status().status == "cancelled"


def test_recurring_invoice_import_stops_after_three_consecutive_failures():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = ZohoBooksRecurringInvoiceImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            books_service=FailingBooksRecurringInvoiceReader(),
        )
        status, started = service.start_all(requested_by="books-admin")
        db.commit()
        assert started is True
        assert status.total_invoices == 4
        assert [service.process_next_invoice() for _ in range(3)] == ["failed", "failed", "stopped"]
        result = service.status()
        assert result is not None
        assert result.status == "stopped"
        assert result.processed_invoices == 3
        assert result.failed_invoices == 3
        assert result.consecutive_failures == 3
