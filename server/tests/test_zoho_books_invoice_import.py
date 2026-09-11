import json
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_documents import HubFinanceInvoice
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.services.hub_finance_documents import INVOICE_MODULE, HubFinanceDocumentService
from app.services.zoho_books_invoice_import import ZohoBooksInvoiceImportService
from app.services.zoho_crm import ZohoBinaryDownload


class FakeBooksInvoiceReader:
    def __init__(self):
        self.invoice = {
            "invoice_id": "9001",
            "invoice_number": "RE-2026-42",
            "status": "sent",
            "date": "2026-09-01",
            "due_date": "2026-09-15",
            "currency_code": "EUR",
            "balance": "29.75",
            "customer_name": "Beispiel GmbH",
            "zcrm_account_id": "crm-customer-1",
            "last_modified_time": "2026-09-01T09:12:00+0200",
            "billing_address": {"address": "Musterstraße 1", "zip": "80331", "city": "München", "country": "Deutschland"},
            "line_items": [{
                "item_id": "books-item-1",
                "name": "Jahresbeitrag Homepage",
                "description": "Wartung und Betreuung",
                "quantity": 1,
                "unit": "Jahr",
                "rate": "25",
                "discount": "0",
                "tax_percentage": 19,
            }],
        }

    @staticmethod
    def get_status():
        return SimpleNamespace(organization_id="books-org-1")

    @staticmethod
    def list_recent_invoice_ids(*, limit: int):
        assert limit == 100
        return ("9001",)

    def get_invoice(self, *, invoice_id: str):
        assert invoice_id == "9001"
        return self.invoice

    @staticmethod
    def download_invoice_pdf(*, invoice_id: str):
        assert invoice_id == "9001"
        return ZohoBinaryDownload(content=b"%PDF-1.7\nZUGFeRD invoice", content_type="application/pdf")


class FakePdfStorage:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.index = 0

    def ensure_ready(self):
        return None

    def store(self, content: bytes) -> str:
        self.index += 1
        key = f"storage-{self.index}"
        self.files[key] = content
        return key

    def load(self, storage_key: str) -> bytes:
        return self.files[storage_key]

    def remove(self, storage_key: str):
        self.files.pop(storage_key, None)


def test_recent_books_invoice_import_is_idempotent_and_stores_the_available_pdf():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(name="Beispiel GmbH", zoho_id="crm-customer-1")
        article = HubFinanceArticle(
            zoho_books_id="books-item-1",
            encrypted_fields_json=cipher.encrypt(json.dumps({"name": "Jahresbeitrag Homepage", "sku": "ART-1"})),
        )
        db.add_all((customer, article))
        db.commit()
        books = FakeBooksInvoiceReader()
        storage = FakePdfStorage()
        service = ZohoBooksInvoiceImportService(db=db, cipher=cipher, books_service=books, pdf_storage=storage)

        status, started = service.start(requested_by="books-admin")
        db.commit()
        assert started is True
        assert status.total_invoices == 1
        assert service.process_next_invoice() == "succeeded"
        assert service.process_next_invoice() == "completed"

        invoice = db.scalar(select(HubFinanceInvoice).where(HubFinanceInvoice.zoho_books_id == "9001"))
        assert invoice is not None
        assert invoice.invoice_number == "RE-2026-42"
        assert invoice.customer_id == customer.id
        assert invoice.lines[0].article_id == article.id
        values = json.loads(cipher.decrypt(invoice.encrypted_fields_json))
        assert values["remaining_amount"] == "29.75"
        assert values["billing_address"] == "Musterstraße 1\n80331 München\nDeutschland"
        pdf = db.scalar(select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == invoice.id))
        assert pdf is not None
        assert pdf.is_zugferd is True
        assert service.get_invoice_pdf(invoice_id=invoice.id).content == b"%PDF-1.7\nZUGFeRD invoice"
        detail = HubFinanceDocumentService(db=db, cipher=cipher).get_detail(module=INVOICE_MODULE, document_id=invoice.id)
        assert detail is not None
        assert detail.lines[0].quantity == "1.00"
        assert detail.invoice_pdf is not None
        assert next(field.value for field in detail.fields if field.key == "remaining_amount") == "29,75 EUR"

        second_status, second_started = service.start(requested_by="books-admin")
        db.commit()
        assert second_started is True
        assert second_status.total_invoices == 1
        assert service.process_next_invoice() == "succeeded"
        assert service.process_next_invoice() == "completed"
        assert db.scalars(select(HubFinanceInvoice).where(HubFinanceInvoice.zoho_books_id == "9001")).all() == [invoice]
        assert service.status().updated_invoices == 1
