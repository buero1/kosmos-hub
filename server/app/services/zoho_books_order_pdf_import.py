"""PDF-only import for orders that already exist in the Hub."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.models.hub_finance_documents import HubFinanceOrder
from app.models.hub_finance_order_pdf import HubFinanceOrderPdf
from app.services.finance_invoice_pdf_storage import FinanceInvoicePdfStorage
from app.services.zoho_books import ZohoBooksService


@dataclass(frozen=True)
class ZohoBooksOrderPdfImportResult:
    total_orders: int
    stored_pdfs: int
    existing_pdfs: int
    unavailable_pdfs: int
    failed_pdfs: int
    stopped_early: bool


class ZohoBooksOrderPdfImportService:
    """Download PDFs only for the current Hub order inventory."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        books_service=None,
        pdf_storage=None,
    ) -> None:
        self.db = db
        self.books = books_service or ZohoBooksService(
            db=db,
            cipher=cipher,
            public_base_url=get_settings().public_base_url,
        )
        self.pdf_storage = pdf_storage or FinanceInvoicePdfStorage(cipher=cipher)

    def current_orders(self) -> tuple[HubFinanceOrder, ...]:
        """Return only Hub orders that already carry a Books source identity."""
        return tuple(
            self.db.scalars(
                select(HubFinanceOrder)
                .where(HubFinanceOrder.zoho_books_id.is_not(None))
                .order_by(HubFinanceOrder.order_number.asc(), HubFinanceOrder.id.asc())
            ).all()
        )

    def import_current_orders(self, *, progress=None) -> ZohoBooksOrderPdfImportResult:
        """Store missing PDFs without modifying any order business data."""
        self.pdf_storage.ensure_ready()
        orders = self.current_orders()
        stored = 0
        existing = 0
        unavailable = 0
        failed = 0
        consecutive_failures = 0
        stopped_early = False

        for index, order in enumerate(orders, start=1):
            current = self.db.scalar(
                select(HubFinanceOrderPdf).where(HubFinanceOrderPdf.order_id == order.id)
            )
            if current is not None:
                existing += 1
                consecutive_failures = 0
                if progress is not None:
                    progress(index, len(orders), order, "existing", None)
                continue
            try:
                self.store_for_order(order=order)
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                message = self._safe_error(exc)
                if "keine PDF-Datei" in message:
                    unavailable += 1
                    consecutive_failures = 0
                    status = "unavailable"
                else:
                    failed += 1
                    consecutive_failures += 1
                    status = "failed"
                if progress is not None:
                    progress(index, len(orders), order, status, message)
                if consecutive_failures >= 3:
                    stopped_early = True
                    break
                continue
            stored += 1
            consecutive_failures = 0
            if progress is not None:
                progress(index, len(orders), order, "stored", None)

        return ZohoBooksOrderPdfImportResult(
            total_orders=len(orders),
            stored_pdfs=stored,
            existing_pdfs=existing,
            unavailable_pdfs=unavailable,
            failed_pdfs=failed,
            stopped_early=stopped_early,
        )

    def store_for_order(self, *, order: HubFinanceOrder) -> HubFinanceOrderPdf:
        source_id = str(order.zoho_books_id or "").strip()
        if not source_id:
            raise ValueError("Der Hub-Auftrag besitzt keine Zoho-Books-ID.")
        download = self.books.download_sales_order_pdf(sales_order_id=source_id)
        storage_key = self.pdf_storage.store(download.content)
        previous = self.db.scalar(
            select(HubFinanceOrderPdf).where(HubFinanceOrderPdf.order_id == order.id)
        )
        previous_key = previous.storage_key if previous is not None else None
        if previous is None:
            previous = HubFinanceOrderPdf(
                order_id=order.id,
                source="zoho-books",
                filename=self._pdf_filename(order.order_number or source_id),
                content_type="application/pdf",
                byte_size=len(download.content),
                storage_key=storage_key,
                imported_at=datetime.now(UTC),
            )
            self.db.add(previous)
        else:
            previous.source = "zoho-books"
            previous.filename = self._pdf_filename(order.order_number or source_id)
            previous.content_type = "application/pdf"
            previous.byte_size = len(download.content)
            previous.storage_key = storage_key
            previous.imported_at = datetime.now(UTC)
        self.db.flush()
        if previous_key and previous_key != storage_key:
            self.pdf_storage.remove(previous_key)
        return previous

    @staticmethod
    def _pdf_filename(order_number: str) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", order_number).strip(".-") or "auftrag"
        return f"{safe_name[:240]}.pdf"

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).replace("Rechnungs-PDF", "Auftrags-PDF").replace("rechnungs-pdf", "auftrags-pdf")
        return message.splitlines()[0][:1_000] or "Das Auftrags-PDF konnte nicht importiert werden."
