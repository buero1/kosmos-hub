"""Background import for all Zoho Books recurring invoices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from threading import Lock
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_documents import HubFinanceRecurringInvoice, HubFinanceRecurringInvoiceLine
from app.models.zoho_books_recurring_invoice_import import (
    ZohoBooksRecurringInvoiceImport,
    ZohoBooksRecurringInvoiceImportItem,
)
from app.services.zoho_books import ZohoBooksError, ZohoBooksService


_ACTIVE_STATUSES = ("pending", "running")
_CENT = Decimal("0.01")
_QUANTITY_STEP = Decimal("0.01")
_import_start_lock = Lock()


class _BooksRecurringInvoiceReader(Protocol):
    def get_status(self): ...

    def list_all_recurring_invoice_ids(self) -> tuple[str, ...]: ...

    def get_recurring_invoice(self, *, recurring_invoice_id: str) -> dict[str, object]: ...

    def get_books_contact(self, *, contact_id: str) -> dict[str, object]: ...


@dataclass(frozen=True)
class ZohoBooksRecurringInvoiceImportStatus:
    id: int
    status: str
    total_invoices: int
    processed_invoices: int
    imported_invoices: int
    updated_invoices: int
    failed_invoices: int
    consecutive_failures: int
    cancel_requested: bool
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None


class ZohoBooksRecurringInvoiceImportService:
    """Persist recurring invoice snapshots one at a time so the job can resume after restarts."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        books_service: _BooksRecurringInvoiceReader | None = None,
    ) -> None:
        self.db = db
        self.cipher = cipher
        self.books = books_service or ZohoBooksService(
            db=db,
            cipher=cipher,
            public_base_url=get_settings().public_base_url,
        )

    def status(self) -> ZohoBooksRecurringInvoiceImportStatus | None:
        run = self._active_import()
        if run is None:
            run = self.db.scalar(
                select(ZohoBooksRecurringInvoiceImport)
                .order_by(ZohoBooksRecurringInvoiceImport.id.desc())
                .limit(1)
            )
        return self._status(run) if run is not None else None

    def start_all(self, *, requested_by: str) -> tuple[ZohoBooksRecurringInvoiceImportStatus, bool]:
        with _import_start_lock:
            active = self._active_import()
            if active is not None:
                return self._status(active), False
            connection_status = self.books.get_status()
            organization_id = str(getattr(connection_status, "organization_id", "") or "").strip()
            if not organization_id:
                raise ZohoBooksError("Wähle zuerst die Zoho-Books-Organisation für den Import aus.")
            source_ids = self.books.list_all_recurring_invoice_ids()
            if not source_ids:
                raise ZohoBooksError("Zoho Books enthält keine periodischen Rechnungen für den Import.")
            run = ZohoBooksRecurringInvoiceImport(
                requested_by=requested_by[:128],
                organization_id=organization_id,
                status="pending",
                total_invoices=len(source_ids),
            )
            self.db.add(run)
            self.db.flush()
            self.db.add_all(
                ZohoBooksRecurringInvoiceImportItem(
                    recurring_invoice_import_id=run.id,
                    zoho_recurring_invoice_id=source_id,
                )
                for source_id in source_ids
            )
            self.db.flush()
            return self._status(run), True

    def cancel(self) -> tuple[ZohoBooksRecurringInvoiceImportStatus | None, bool]:
        run = self._active_import()
        if run is None:
            return self.status(), False
        run.cancel_requested = True
        self.db.flush()
        return self._status(run), True

    def process_next_invoice(self) -> str | None:
        """Import one recurring invoice without keeping a database transaction open during API I/O."""
        run = self._active_import()
        if run is None:
            return None
        if run.cancel_requested:
            run.status = "cancelled"
            run.completed_at = datetime.now(UTC)
            run.last_error = "Der Import periodischer Rechnungen wurde durch den Benutzer abgebrochen."
            self.db.commit()
            return "cancelled"
        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC)

        item = self.db.scalar(
            select(ZohoBooksRecurringInvoiceImportItem)
            .where(
                ZohoBooksRecurringInvoiceImportItem.recurring_invoice_import_id == run.id,
                ZohoBooksRecurringInvoiceImportItem.status == "pending",
            )
            .order_by(ZohoBooksRecurringInvoiceImportItem.id.asc())
            .limit(1)
        )
        if item is None:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            self.db.commit()
            return "completed"

        run_id = run.id
        item_id = item.id
        source_id = item.zoho_recurring_invoice_id
        self.db.commit()
        try:
            payload = self.books.get_recurring_invoice(recurring_invoice_id=source_id)
            recurring_invoice, was_created = self._upsert_recurring_invoice(
                payload=payload,
                source_id=source_id,
            )
        except (ZohoBooksError, ValueError) as exc:
            self.db.rollback()
            stopped = self._record_failure(run_id=run_id, item_id=item_id, error=str(exc))
            return "stopped" if stopped else "failed"
        except Exception:
            self.db.rollback()
            stopped = self._record_failure(
                run_id=run_id,
                item_id=item_id,
                error="Die periodische Rechnung konnte nicht vollständig aus Zoho Books importiert werden.",
            )
            return "stopped" if stopped else "failed"

        run = self.db.get(ZohoBooksRecurringInvoiceImport, run_id)
        item = self.db.get(ZohoBooksRecurringInvoiceImportItem, item_id)
        if run is None or item is None:
            self.db.rollback()
            return None
        item.local_recurring_invoice_id = recurring_invoice.id
        item.status = "imported"
        item.last_error = None
        run.processed_invoices += 1
        if was_created:
            run.imported_invoices += 1
        else:
            run.updated_invoices += 1
        run.consecutive_failures = 0
        run.last_error = None
        self.db.commit()
        return "succeeded"

    def _upsert_recurring_invoice(
        self,
        *,
        payload: dict[str, object],
        source_id: str,
    ) -> tuple[HubFinanceRecurringInvoice, bool]:
        returned_source_id = self._text(payload.get("recurring_invoice_id"))
        if returned_source_id and returned_source_id != source_id:
            raise ZohoBooksError("Zoho Books hat eine nicht passende periodische Rechnung zurückgegeben.")
        recurring_invoice = self.db.scalar(
            select(HubFinanceRecurringInvoice).where(HubFinanceRecurringInvoice.zoho_books_id == source_id)
        )
        was_created = recurring_invoice is None
        if recurring_invoice is None:
            recurring_invoice = HubFinanceRecurringInvoice(
                zoho_books_id=source_id,
                encrypted_fields_json=self._encrypt({}),
            )
            self.db.add(recurring_invoice)
            self.db.flush()

        customer = self._matching_customer(payload)
        books_contact: dict[str, object] | None = None
        if customer is None:
            books_customer_id = self._text(payload.get("customer_id"))
            if books_customer_id:
                books_contact = self.books.get_books_contact(contact_id=books_customer_id)
                customer = self._matching_customer(books_contact)
        contact = self._matching_contact(payload, customer=customer, books_contact=books_contact)
        recurring_invoice.customer = customer
        recurring_invoice.contact = contact
        recurring_invoice.encrypted_fields_json = self._encrypt(self._recurring_invoice_values(payload))
        recurring_invoice.zoho_modified_at = self._timestamp(payload.get("last_modified_time"))
        recurring_invoice.zoho_imported_at = datetime.now(UTC)
        self._replace_lines(recurring_invoice=recurring_invoice, payload=payload)
        self.db.flush()
        return recurring_invoice, was_created

    def _replace_lines(
        self,
        *,
        recurring_invoice: HubFinanceRecurringInvoice,
        payload: dict[str, object],
    ) -> None:
        for line in tuple(recurring_invoice.lines):
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
                "sku": self._text(raw_line.get("sku"))
                or self._text(raw_line.get("item_code"))
                or article_values.get("sku", ""),
                "description": description,
                "quantity": self._decimal_text(raw_line.get("quantity"), default="1", places=_QUANTITY_STEP),
                "unit": self._text(raw_line.get("unit")),
                "unit_price": self._decimal_text(raw_line.get("rate"), default="0", places=_CENT),
                "discount_percent": self._discount_percent(raw_line),
                "tax_rate": self._tax_rate(raw_line.get("tax_percentage")),
            }
            recurring_invoice.lines.append(
                HubFinanceRecurringInvoiceLine(
                    article=article,
                    position_index=index,
                    encrypted_fields_json=self._encrypt(values),
                )
            )

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
            for customer in self.db.scalars(select(Customer)).all()
            if customer.name.strip().casefold() == name
        ]
        return matches[0] if len(matches) == 1 else None

    def _matching_contact(
        self,
        payload: dict[str, object],
        *,
        customer: Customer | None,
        books_contact: dict[str, object] | None,
    ) -> CustomerContact | None:
        source_ids = [
            self._text(payload.get(key))
            for key in ("zcrm_contact_id", "contact_person_id", "contact_id")
        ]
        associated = payload.get("contact_persons_associated")
        if isinstance(associated, list):
            source_ids.extend(
                self._text(item.get("contact_person_id"))
                for item in associated
                if isinstance(item, dict)
            )
        raw_contact_ids = payload.get("contact_persons")
        if isinstance(raw_contact_ids, list):
            source_ids.extend(self._text(value) for value in raw_contact_ids)
        associated_ids = {source_id for source_id in source_ids if source_id}
        books_people = books_contact.get("contact_persons") if isinstance(books_contact, dict) else None
        if isinstance(books_people, list):
            for person in books_people:
                if not isinstance(person, dict):
                    continue
                books_person_id = self._text(person.get("contact_person_id"))
                if associated_ids and books_person_id not in associated_ids:
                    continue
                source_ids.append(self._text(person.get("zcrm_contact_id")))
        for source_id in source_ids:
            if not source_id:
                continue
            contact = self.db.scalar(select(CustomerContact).where(CustomerContact.zoho_id == source_id))
            if contact is not None and (customer is None or contact.customer_id == customer.id):
                return contact

        email_addresses = set()
        if isinstance(associated, list):
            email_addresses = {
                self._text(item.get("contact_person_email")).casefold()
                for item in associated
                if isinstance(item, dict) and self._text(item.get("contact_person_email"))
            }
        if isinstance(books_people, list):
            email_addresses.update(
                self._text(person.get("email")).casefold()
                for person in books_people
                if isinstance(person, dict)
                and (not associated_ids or self._text(person.get("contact_person_id")) in associated_ids)
                and self._text(person.get("email"))
            )
        if not email_addresses:
            return None
        query = select(CustomerContact)
        if customer is not None:
            query = query.where(CustomerContact.customer_id == customer.id)
        matches = []
        for contact in self.db.scalars(query).all():
            values = self._contact_values(contact)
            stored_addresses = {
                self._text(values.get(key)).casefold()
                for key in ("E-Mail", "Zweite E-Mail-Adresse", "Dritte E-Mail-Adresse")
                if self._text(values.get(key))
            }
            if stored_addresses & email_addresses:
                matches.append(contact)
        return matches[0] if len(matches) == 1 else None

    def _matching_article(self, raw_line: dict[str, object]) -> tuple[HubFinanceArticle | None, dict[str, str]]:
        source_id = self._text(raw_line.get("item_id"))
        if source_id:
            article = self.db.scalar(
                select(HubFinanceArticle).where(HubFinanceArticle.zoho_books_id == source_id)
            )
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

    def _recurring_invoice_values(self, payload: dict[str, object]) -> dict[str, str]:
        interval_unit, interval_count = self._recurrence(payload)
        custom_values = payload.get("custom_field_hash")
        custom_values = custom_values if isinstance(custom_values, dict) else {}
        recurrence_preferences = self._text(payload.get("recurrence_preferences")).casefold()
        return {
            "name": self._text(payload.get("recurrence_name")),
            "status": self._status_value(payload.get("status")),
            "start_date": self._date_text(payload.get("start_date")),
            "end_date": self._date_text(payload.get("end_date")),
            "interval_unit": interval_unit,
            "interval_count": str(interval_count),
            "next_invoice_date": self._date_text(payload.get("next_invoice_date")),
            "automatic_creation": "false" if recurrence_preferences == "save_as_draft" else "true",
            "currency": self._text(payload.get("currency_code")) or "EUR",
            "payment_terms": self._payment_terms(payload.get("payment_terms")),
            "late_fee": self._decimal_text(
                custom_values.get("cf_mahngeb_unformatted"),
                default="0",
                places=_CENT,
            ),
            "system_payment_complete": self._bool_text(
                custom_values.get("cf_systemzahlung_erledigt_unformatted")
            ),
            "reference_number": self._text(payload.get("reference_number")),
            "notes": self._text(payload.get("notes")),
            "terms": self._text(payload.get("terms")),
            "template_id": self._text(payload.get("template_id")),
            "template_name": self._text(payload.get("template_name")),
            "zoho_recurrence_frequency": self._text(payload.get("recurrence_frequency")),
            "zoho_repeat_every": self._text(payload.get("repeat_every")),
            "zoho_recurrence_preferences": self._text(payload.get("recurrence_preferences")),
        }

    @staticmethod
    def _recurrence(payload: dict[str, object]) -> tuple[str, int]:
        frequency = ZohoBooksRecurringInvoiceImportService._text(
            payload.get("recurrence_frequency")
        ).casefold()
        try:
            repeat_every = max(1, int(str(payload.get("repeat_every") or "1")))
        except ValueError:
            repeat_every = 1
        if frequency in {"year", "years", "yearly"}:
            return "year", repeat_every
        if frequency in {"month", "months", "monthly"} and repeat_every % 3 == 0:
            return "quarter", max(1, repeat_every // 3)
        return "month", repeat_every

    @staticmethod
    def _status_value(value: object) -> str:
        status = ZohoBooksRecurringInvoiceImportService._text(value).casefold()
        if status in {"stopped", "paused"}:
            return "paused"
        if status in {"expired", "ended", "completed", "cancelled", "canceled"}:
            return "ended"
        return "active"

    @staticmethod
    def _payment_terms(value: object) -> str:
        try:
            days = int(str(value or "0"))
        except ValueError:
            return ""
        return {
            0: "due_on_receipt",
            7: "7_days",
            14: "14_days",
            30: "30_days",
        }.get(days, "")

    @staticmethod
    def _free_text_values(
        *,
        article: HubFinanceArticle | None,
        name: str,
        description: str,
    ) -> tuple[str, str]:
        if article is None and name.strip().casefold() in {"freitextposition", "freitext position"} and description.strip():
            return description, ""
        return name, description

    @staticmethod
    def _discount_percent(raw_line: dict[str, object]) -> str:
        raw = ZohoBooksRecurringInvoiceImportService._text(raw_line.get("discount")).strip()
        if raw.endswith("%"):
            raw = raw[:-1].strip()
        value = ZohoBooksRecurringInvoiceImportService._decimal_text(raw, default="0", places=_CENT)
        try:
            return value if Decimal(value) <= Decimal("100") else "0.00"
        except InvalidOperation:
            return "0.00"

    @staticmethod
    def _tax_rate(value: object) -> str:
        decimal_value = ZohoBooksRecurringInvoiceImportService._decimal_text(
            value,
            default="19",
            places=_CENT,
        )
        if decimal_value in {"0.00", "0"}:
            return "0"
        if decimal_value in {"7.00", "7"}:
            return "7"
        return "19"

    def _contact_values(self, contact: CustomerContact) -> dict[str, object]:
        try:
            profile = json.loads(self.cipher.decrypt(contact.encrypted_profile_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        values = profile.get("fields") if isinstance(profile, dict) else None
        return values if isinstance(values, dict) else {}

    def _record_failure(self, *, run_id: int, item_id: int, error: str) -> bool:
        run = self.db.get(ZohoBooksRecurringInvoiceImport, run_id)
        item = self.db.get(ZohoBooksRecurringInvoiceImportItem, item_id)
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

    def _active_import(self) -> ZohoBooksRecurringInvoiceImport | None:
        return self.db.scalar(
            select(ZohoBooksRecurringInvoiceImport)
            .where(ZohoBooksRecurringInvoiceImport.status.in_(_ACTIVE_STATUSES))
            .order_by(ZohoBooksRecurringInvoiceImport.id.asc())
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
    def _decimal_text(value: object, *, default: str, places: Decimal) -> str:
        raw = ZohoBooksRecurringInvoiceImportService._text(value).replace(",", ".").strip()
        if not raw:
            raw = default
        try:
            decimal_value = Decimal(raw).quantize(places, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            decimal_value = Decimal(default).quantize(places, rounding=ROUND_HALF_UP)
        return format(decimal_value, "f")

    @staticmethod
    def _date_text(value: object) -> str:
        text = ZohoBooksRecurringInvoiceImportService._text(value)
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        text = ZohoBooksRecurringInvoiceImportService._text(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _bool_text(value: object) -> str:
        return "true" if value is True or str(value).strip().casefold() in {"1", "true", "yes", "ja"} else "false"

    @staticmethod
    def _text(value: object) -> str:
        return str(value).strip() if isinstance(value, (str, int, float, Decimal)) else ""

    @staticmethod
    def _safe_message(error: str) -> str:
        if "Zoho rejected the request" in error:
            return error.splitlines()[0][:1_000]
        return "Die periodische Rechnung konnte nicht vollständig aus Zoho Books importiert werden."

    @staticmethod
    def _status(run: ZohoBooksRecurringInvoiceImport) -> ZohoBooksRecurringInvoiceImportStatus:
        return ZohoBooksRecurringInvoiceImportStatus(
            id=run.id,
            status=run.status,
            total_invoices=run.total_invoices,
            processed_invoices=run.processed_invoices,
            imported_invoices=run.imported_invoices,
            updated_invoices=run.updated_invoices,
            failed_invoices=run.failed_invoices,
            consecutive_failures=run.consecutive_failures,
            cancel_requested=run.cancel_requested,
            started_at=run.started_at,
            completed_at=run.completed_at,
            last_error=run.last_error,
        )
