from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_finance_documents import HubFinanceOrder
from app.models.hub_finance_order_pdf import HubFinanceOrderPdf
from app.services.hub_finance_documents import HubFinanceDocumentService, ORDER_MODULE
from app.services.zoho_books_order_pdf_import import ZohoBooksOrderPdfImportService
from app.services.zoho_crm import ZohoBinaryDownload


class FakeBooksOrderPdfReader:
    def __init__(self) -> None:
        self.requested_ids: list[str] = []

    def download_sales_order_pdf(self, *, sales_order_id: str) -> ZohoBinaryDownload:
        self.requested_ids.append(sales_order_id)
        return ZohoBinaryDownload(content=b"%PDF-1.7\norder", content_type="application/pdf")


class FakePdfStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def ensure_ready(self) -> None:
        return None

    def store(self, content: bytes) -> str:
        key = f"order-pdf-{len(self.files) + 1}"
        self.files[key] = content
        return key

    def load(self, storage_key: str) -> bytes:
        return self.files[storage_key]

    def remove(self, storage_key: str) -> None:
        self.files.pop(storage_key, None)


def test_pdf_only_import_uses_current_hub_orders_and_keeps_order_data_unchanged():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        imported = HubFinanceOrder(
            order_number="SO-100",
            zoho_books_id="7001",
            encrypted_fields_json=cipher.encrypt('{"order_name":"Website"}'),
        )
        local = HubFinanceOrder(
            order_number="AU-LOCAL",
            encrypted_fields_json=cipher.encrypt('{"order_name":"Lokaler Auftrag"}'),
        )
        db.add_all((imported, local))
        db.commit()
        original_values = imported.encrypted_fields_json
        books = FakeBooksOrderPdfReader()
        storage = FakePdfStorage()
        service = ZohoBooksOrderPdfImportService(
            db=db,
            cipher=cipher,
            books_service=books,
            pdf_storage=storage,
        )

        result = service.import_current_orders()

        assert result.total_orders == 1
        assert result.stored_pdfs == 1
        assert result.existing_pdfs == 0
        assert result.unavailable_pdfs == 0
        assert result.failed_pdfs == 0
        assert books.requested_ids == ["7001"]
        assert imported.encrypted_fields_json == original_values
        pdf = db.scalar(select(HubFinanceOrderPdf))
        assert pdf is not None
        assert pdf.order_id == imported.id
        assert pdf.filename == "SO-100.pdf"

        detail = HubFinanceDocumentService(db=db, cipher=cipher).get_detail(
            module=ORDER_MODULE,
            document_id=imported.id,
        )
        assert detail is not None
        assert detail.order_pdf is not None
        assert detail.order_pdf.filename == "SO-100.pdf"

        second = service.import_current_orders()
        assert second.stored_pdfs == 0
        assert second.existing_pdfs == 1
        assert books.requested_ids == ["7001"]
