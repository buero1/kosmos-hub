"""Background import for a bounded set of recent Zoho Books invoices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_documents import HubFinanceInvoice, HubFinanceInvoiceLine
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.models.zoho_books_invoice_import import ZohoBooksInvoiceImport, ZohoBooksInvoiceImportItem
from app.services.finance_invoice_pdf_storage import FinanceInvoicePdfStorage, FinanceInvoicePdfStorageError
from app.services.zoho_books import ZohoBooksError, ZohoBooksService
from app.services.zoho_crm import ZohoBinaryDownload


_ACTIVE_STATUSES = ("pending", "running")
_CENT = Decimal("0.01")
_QUANTITY_STEP = Decimal("0.01")
_ZUGFERD_MARKERS = (b"zugferd", b"factur-x", b"crossindustryinvoice")


class _BooksInvoiceReader(Protocol):
    def get_status(self): ...

    def list_recent_invoice_ids(self, *, limit: int) -> tuple[str, ...]: ...

    def get_invoice(self, *, invoice_id: str) -> dict[str, object]: ...

    def download_invoice_pdf(self, *, invoice_id: str) -> ZohoBinaryDownload: ...


@dataclass(frozen=True)
class ZohoBooksInvoiceImportStatus:
    id: int
    status: str
    requested_limit: int
    total_invoices: int
    processed_invoices: int
    imported_invoices: int
    updated_invoices: int
    stored_pdfs: int
    unavailable_pdfs: int
    failed_invoices: int
    consecutive_failures: int
    cancel_requested: bool
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class FinanceInvoicePdfDownload:
    filename: str
    content_type: str
    content: bytes


class ZohoBooksInvoiceImportService:
    """Persist Books invoice records one at a time so the job is restart-safe."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        books_service: _BooksInvoiceReader | None = None,
        pdf_storage: FinanceInvoicePdfStorage | None = None,
    ) -> None:
        self.db = db
        self.cipher = cipher
        self.books = books_service or ZohoBooksService(
            db=db,
            cipher=cipher,
            public_base_url=get_settings().public_base_url,
        )
        self.pdf_storage = pdf_storage or FinanceInvoicePdfStorage(cipher=cipher)

    def status(self) -> ZohoBooksInvoiceImportStatus | None:
        run = self.db.scalar(select(ZohoBooksInvoiceImport).order_by(ZohoBooksInvoiceImport.id.desc()).limit(1))
        return self._status(run) if run is not None else None

    def start(self, *, requested_by: str, limit: int = 100) -> tuple[ZohoBooksInvoiceImportStatus, bool]:
        if limit != 100:
            raise ValueError("Der erste Rechnungsimport umfasst genau die 100 jüngsten Rechnungen.")
        active = self._active_import()
        if active is not None:
            return self._status(active), False

        self.pdf_storage.ensure_ready()
        status = self.books.get_status()
        organization_id = str(getattr(status, "organization_id", "") or "").strip()
        if not organization_id:
            raise ZohoBooksError("Wähle zuerst die Zoho-Books-Organisation für den Import aus.")
        source_ids = self.books.list_recent_invoice_ids(limit=limit)
        if not source_ids:
            raise ZohoBooksError("Zoho Books enthält keine Rechnungen für den Import.")

        run = ZohoBooksInvoiceImport(
            requested_by=requested_by[:128],
            organization_id=organization_id,
            status="pending",
            requested_limit=limit,
            total_invoices=len(source_ids),
        )
        self.db.add(run)
        self.db.flush()
        self.db.add_all(
            ZohoBooksInvoiceImportItem(invoice_import_id=run.id, zoho_invoice_id=source_id)
            for source_id in source_ids
        )
        self.db.flush()
        return self._status(run), True

    def cancel(self) -> tuple[ZohoBooksInvoiceImportStatus | None, bool]:
        run = self._active_import()
        if run is None:
            return self.status(), False
        run.cancel_requested = True
        self.db.flush()
        return self._status(run), True

    def process_next_invoice(self) -> str | None:
        """Import one invoice and its PDF without keeping a transaction open during I/O."""
        run = self._active_import()
        if run is None:
            return None
        if run.cancel_requested:
            run.status = "cancelled"
            run.completed_at = datetime.now(UTC)
            run.last_error = "Der Rechnungsimport wurde durch den Benutzer abgebrochen."
            self.db.commit()
            return "cancelled"
        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC)

        item = self.db.scalar(
            select(ZohoBooksInvoiceImportItem)
            .where(
                ZohoBooksInvoiceImportItem.invoice_import_id == run.id,
                ZohoBooksInvoiceImportItem.status == "pending",
            )
            .order_by(ZohoBooksInvoiceImportItem.id.asc())
            .limit(1)
        )
        if item is None:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            self.db.commit()
            return "completed"

        run_id = run.id
        item_id = item.id
        source_id = item.zoho_invoice_id
        self.db.commit()
        try:
            payload = self.books.get_invoice(invoice_id=source_id)
            invoice, was_created = self._upsert_invoice(payload=payload, source_id=source_id)
            pdf_status, pdf_error = self._store_pdf(invoice=invoice, source_id=source_id)
        except (FinanceInvoicePdfStorageError, ZohoBooksError, ValueError) as exc:
            self.db.rollback()
            stopped = self._record_failure(run_id=run_id, item_id=item_id, error=str(exc))
            return "stopped" if stopped else "failed"
        except Exception:
            self.db.rollback()
            stopped = self._record_failure(
                run_id=run_id,
                item_id=item_id,
                error="Die Rechnung konnte nicht vollständig aus Zoho Books importiert werden.",
            )
            return "stopped" if stopped else "failed"

        run = self.db.get(ZohoBooksInvoiceImport, run_id)
        item = self.db.get(ZohoBooksInvoiceImportItem, item_id)
        if run is None or item is None:
            self.db.rollback()
            return None
        item.local_invoice_id = invoice.id
        item.status = "imported"
        item.pdf_status = pdf_status
        item.last_error = pdf_error
        run.processed_invoices += 1
        if was_created:
            run.imported_invoices += 1
        else:
            run.updated_invoices += 1
        if pdf_status == "stored":
            run.stored_pdfs += 1
        else:
            run.unavailable_pdfs += 1
        run.consecutive_failures = 0
        run.last_error = pdf_error
        self.db.commit()
        return "succeeded"

    def get_invoice_pdf(self, *, invoice_id: int) -> FinanceInvoicePdfDownload:
        record = self.db.scalar(
            select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == invoice_id)
        )
        if record is None:
            raise ValueError("Für diese Rechnung ist noch keine PDF-Vorschau vorhanden.")
        return FinanceInvoicePdfDownload(
            filename=record.filename,
            content_type=record.content_type,
            content=self.pdf_storage.load(record.storage_key),
        )

    def _upsert_invoice(self, *, payload: dict[str, object], source_id: str) -> tuple[HubFinanceInvoice, bool]:
        returned_source_id = self._text(payload.get("invoice_id"))
        if returned_source_id and returned_source_id != source_id:
            raise ZohoBooksError("Zoho Books hat eine nicht passende Rechnung zurückgegeben.")
        invoice = self.db.scalar(select(HubFinanceInvoice).where(HubFinanceInvoice.zoho_books_id == source_id))
        was_created = invoice is None
        if invoice is None:
            invoice = HubFinanceInvoice(zoho_books_id=source_id, encrypted_fields_json=self._encrypt({}))
            self.db.add(invoice)
            self.db.flush()

        customer = self._matching_customer(payload)
        contact = self._matching_contact(payload, customer=customer)
        invoice.customer = customer
        invoice.contact = contact
        invoice.order = None
        invoice.invoice_number = self._available_invoice_number(
            invoice=invoice,
            number=self._text(payload.get("invoice_number")) or f"Zoho-{source_id}",
            source_id=source_id,
        )
        invoice.encrypted_fields_json = self._encrypt(self._invoice_values(payload))
        invoice.zoho_modified_at = self._timestamp(payload.get("last_modified_time"))
        invoice.zoho_imported_at = datetime.now(UTC)
        self._replace_lines(invoice=invoice, payload=payload)
        self.db.flush()
        return invoice, was_created

    def _store_pdf(self, *, invoice: HubFinanceInvoice, source_id: str) -> tuple[str, str | None]:
        try:
            download = self.books.download_invoice_pdf(invoice_id=source_id)
        except ZohoBooksError as exc:
            return "unavailable", self._safe_message(str(exc))

        storage_key = self.pdf_storage.store(download.content)
        previous = self.db.scalar(select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == invoice.id))
        previous_key = previous.storage_key if previous is not None else None
        if previous is None:
            previous = HubFinanceInvoicePdf(
                invoice_id=invoice.id,
                source="zoho-books",
                filename=self._pdf_filename(invoice.invoice_number or source_id),
                content_type="application/pdf",
                byte_size=len(download.content),
                storage_key=storage_key,
                is_zugferd=self._is_zugferd(download.content),
                imported_at=datetime.now(UTC),
            )
            self.db.add(previous)
        else:
            previous.source = "zoho-books"
            previous.filename = self._pdf_filename(invoice.invoice_number or source_id)
            previous.content_type = "application/pdf"
            previous.byte_size = len(download.content)
            previous.storage_key = storage_key
            previous.is_zugferd = self._is_zugferd(download.content)
            previous.imported_at = datetime.now(UTC)
        self.db.flush()
        if previous_key and previous_key != storage_key:
            self.pdf_storage.remove(previous_key)
        return "stored", None

    def _replace_lines(self, *, invoice: HubFinanceInvoice, payload: dict[str, object]) -> None:
        for line in tuple(invoice.lines):
            self.db.delete(line)
        self.db.flush()
        line_items = payload.get("line_items")
        if not isinstance(line_items, list):
            line_items = []
        for index, raw_line in enumerate(line_items):
            if not isinstance(raw_line, dict):
                continue
            article, article_values = self._matching_article(raw_line)
            name, description = self._free_text_values(
                article=article,
                name=self._text(raw_line.get("name")),
                description=self._text(raw_line.get("description")),
            )
            values = {
                "name": name,
                "sku": self._text(raw_line.get("sku")) or article_values.get("sku", ""),
                "description": description,
                "quantity": self._decimal_text(raw_line.get("quantity"), default="1", places=_QUANTITY_STEP),
                "unit": self._text(raw_line.get("unit")),
                "unit_price": self._decimal_text(raw_line.get("rate"), default="0", places=_CENT),
                "discount_percent": self._discount_percent(raw_line),
                "tax_rate": self._tax_rate(raw_line.get("tax_percentage")),
            }
            invoice.lines.append(HubFinanceInvoiceLine(
                article=article,
                position_index=index,
                encrypted_fields_json=self._encrypt(values),
            ))

    @staticmethod
    def _free_text_values(*, article: HubFinanceArticle | None, name: str, description: str) -> tuple[str, str]:
        """Put Books free text into the position name instead of retaining a generic placeholder."""
        if article is None and name.strip().casefold() in {"freitextposition", "freitext position"} and description.strip():
            return description, ""
        return name, description

    def _matching_customer(self, payload: dict[str, object]) -> Customer | None:
        for key in ("zcrm_account_id", "crm_account_id", "customer_id"):
            source_id = self._text(payload.get(key))
            if source_id:
                customer = self.db.scalar(select(Customer).where(Customer.zoho_id == source_id))
                if customer is not None:
                    return customer
        name = self._text(payload.get("customer_name")).casefold()
        if not name:
            return None
        matches = [
            customer
            for customer in self.db.scalars(select(Customer).where(Customer.is_visible.is_(True))).all()
            if customer.name.strip().casefold() == name
        ]
        return matches[0] if len(matches) == 1 else None

    def _matching_contact(self, payload: dict[str, object], *, customer: Customer | None) -> CustomerContact | None:
        contact_ids = [self._text(payload.get(key)) for key in ("zcrm_contact_id", "contact_person_id", "contact_id")]
        for source_id in contact_ids:
            if not source_id:
                continue
            contact = self.db.scalar(select(CustomerContact).where(CustomerContact.zoho_id == source_id))
            if contact is not None and (customer is None or contact.customer_id == customer.id):
                return contact
        return None

    def _matching_article(self, raw_line: dict[str, object]) -> tuple[HubFinanceArticle | None, dict[str, str]]:
        source_id = self._text(raw_line.get("item_id"))
        if source_id:
            article = self.db.scalar(select(HubFinanceArticle).where(HubFinanceArticle.zoho_books_id == source_id))
            if article is not None:
                return article, self._decrypt(article.encrypted_fields_json)
        name = self._text(raw_line.get("name")).casefold()
        if not name:
            return None, {}
        matches = []
        for article in self.db.scalars(select(HubFinanceArticle)).all():
            values = self._decrypt(article.encrypted_fields_json)
            if values.get("name", "").strip().casefold() == name:
                matches.append((article, values))
        return matches[0] if len(matches) == 1 else (None, {})

    def _invoice_values(self, payload: dict[str, object]) -> dict[str, str]:
        invoice_date = self._date_text(payload.get("date"))
        due_date = self._date_text(payload.get("due_date")) or invoice_date
        return {
            "status": self._status_value(payload.get("status")),
            "invoice_date": invoice_date,
            "due_date": due_date,
            "currency": self._text(payload.get("currency_code")) or "EUR",
            "payment_terms": self._payment_terms(invoice_date=invoice_date, due_date=due_date),
            "remaining_amount": self._decimal_text(payload.get("balance"), default="0", places=_CENT),
            "billing_address": self._billing_address(payload.get("billing_address")),
        }

    def _available_invoice_number(self, *, invoice: HubFinanceInvoice, number: str, source_id: str) -> str:
        existing = self.db.scalar(select(HubFinanceInvoice).where(HubFinanceInvoice.invoice_number == number))
        if existing is None or existing.id == invoice.id:
            return number[:255]
        return f"{number[:220]} (Zoho {source_id[-24:]})"

    def _record_failure(self, *, run_id: int, item_id: int, error: str) -> bool:
        run = self.db.get(ZohoBooksInvoiceImport, run_id)
        item = self.db.get(ZohoBooksInvoiceImportItem, item_id)
        if run is None or item is None:
            self.db.rollback()
            return False
        message = self._safe_message(error)
        item.status = "failed"
        item.last_error = message
        run.processed_invoices += 1
        run.failed_invoices += 1
        run.consecutive_failures += 1
        stopped = run.consecutive_failures >= 3
        if stopped:
            run.status = "stopped"
            run.completed_at = datetime.now(UTC)
            run.last_error = "Import nach drei aufeinanderfolgenden Fehlern automatisch angehalten."
        else:
            run.last_error = message
        self.db.commit()
        return stopped

    def _active_import(self) -> ZohoBooksInvoiceImport | None:
        return self.db.scalar(
            select(ZohoBooksInvoiceImport)
            .where(ZohoBooksInvoiceImport.status.in_(_ACTIVE_STATUSES))
            .order_by(ZohoBooksInvoiceImport.id.asc())
            .limit(1)
        )

    def _encrypt(self, values: dict[str, str]) -> str:
        return self.cipher.encrypt(json.dumps(values, ensure_ascii=False, separators=(",", ":")))

    def _decrypt(self, encrypted_values: str) -> dict[str, str]:
        try:
            values = json.loads(self.cipher.decrypt(encrypted_values))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return {str(key): self._text(value) for key, value in values.items()} if isinstance(values, dict) else {}

    @staticmethod
    def _status_value(value: object) -> str:
        status = ZohoBooksInvoiceImportService._text(value).casefold()
        if status == "draft":
            return "draft"
        if status == "paid":
            return "paid"
        if status == "overdue":
            return "overdue"
        if status in {"void", "voided", "cancelled", "canceled"}:
            return "cancelled"
        return "open"

    @staticmethod
    def _payment_terms(*, invoice_date: str, due_date: str) -> str:
        try:
            days = (datetime.fromisoformat(due_date).date() - datetime.fromisoformat(invoice_date).date()).days
        except ValueError:
            return ""
        return {0: "due_on_receipt", 14: "14_days", 30: "30_days"}.get(days, "")

    @staticmethod
    def _billing_address(value: object) -> str:
        if not isinstance(value, dict):
            return ""
        lines = []
        for key in ("attention", "address", "street2"):
            text = ZohoBooksInvoiceImportService._text(value.get(key))
            if text:
                lines.append(text)
        city_line = " ".join(part for part in (ZohoBooksInvoiceImportService._text(value.get("zip")), ZohoBooksInvoiceImportService._text(value.get("city"))) if part)
        if city_line:
            lines.append(city_line)
        for key in ("state", "country"):
            text = ZohoBooksInvoiceImportService._text(value.get(key))
            if text and text not in lines:
                lines.append(text)
        return "\n".join(lines)

    @staticmethod
    def _discount_percent(raw_line: dict[str, object]) -> str:
        value = ZohoBooksInvoiceImportService._decimal_text(raw_line.get("discount"), default="0", places=_CENT)
        try:
            return value if Decimal(value) <= Decimal("100") else "0.00"
        except InvalidOperation:
            return "0.00"

    @staticmethod
    def _tax_rate(value: object) -> str:
        decimal_value = ZohoBooksInvoiceImportService._decimal_text(value, default="19", places=_CENT)
        if decimal_value in {"0.00", "0"}:
            return "0"
        if decimal_value in {"7.00", "7"}:
            return "7"
        return "19"

    @staticmethod
    def _decimal_text(value: object, *, default: str, places: Decimal) -> str:
        raw = ZohoBooksInvoiceImportService._text(value).replace(",", ".").strip()
        if not raw:
            raw = default
        try:
            decimal_value = Decimal(raw).quantize(places, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            decimal_value = Decimal(default).quantize(places, rounding=ROUND_HALF_UP)
        return format(decimal_value, "f")

    @staticmethod
    def _date_text(value: object) -> str:
        text = ZohoBooksInvoiceImportService._text(value)
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        text = ZohoBooksInvoiceImportService._text(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _pdf_filename(invoice_number: str) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", invoice_number).strip(".-") or "rechnung"
        return f"{safe_name[:240]}.pdf"

    @staticmethod
    def _is_zugferd(content: bytes) -> bool:
        lowered = content.lower()
        return any(marker in lowered for marker in _ZUGFERD_MARKERS)

    @staticmethod
    def _text(value: object) -> str:
        return str(value).strip() if isinstance(value, (str, int, float, Decimal)) else ""

    @staticmethod
    def _safe_message(error: str) -> str:
        if "nicht genug freier Speicherplatz" in error:
            return error
        if "keine PDF-Datei" in error:
            return error
        if "Zoho rejected the request" in error:
            return error.splitlines()[0][:1_000]
        return "Die Rechnung konnte nicht vollständig aus Zoho Books importiert werden."

    @staticmethod
    def _status(run: ZohoBooksInvoiceImport) -> ZohoBooksInvoiceImportStatus:
        return ZohoBooksInvoiceImportStatus(
            id=run.id,
            status=run.status,
            requested_limit=run.requested_limit,
            total_invoices=run.total_invoices,
            processed_invoices=run.processed_invoices,
            imported_invoices=run.imported_invoices,
            updated_invoices=run.updated_invoices,
            stored_pdfs=run.stored_pdfs,
            unavailable_pdfs=run.unavailable_pdfs,
            failed_invoices=run.failed_invoices,
            consecutive_failures=run.consecutive_failures,
            cancel_requested=run.cancel_requested,
            started_at=run.started_at,
            completed_at=run.completed_at,
            last_error=run.last_error,
        )
