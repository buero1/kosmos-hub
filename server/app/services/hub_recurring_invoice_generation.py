"""Create Hub invoice drafts from due recurring invoices."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
import json
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import SessionLocal
from app.models.hub_finance_documents import HubFinanceInvoice, HubFinanceRecurringInvoice
from app.services.audit import write_audit_log
from app.services.hub_finance_documents import HubFinanceDocumentService, INVOICE_MODULE
from app.services.hub_finance_pdf_generation import HubFinancePdfService


logger = logging.getLogger(__name__)
_BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class RecurringInvoiceRunResult:
    created_ids: tuple[int, ...]
    failed_ids: tuple[int, ...]
    existing_count: int = 0


def _add_months(value: date, months: int, *, anchor_day: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    return date(year, month, min(anchor_day, monthrange(year, month)[1]))


def next_recurring_date(current: date, values: dict[str, str]) -> date:
    rhythm = values.get("interval_unit", "")
    if rhythm == "custom":
        count = int(values.get("custom_interval_count") or "0")
        unit = values.get("custom_interval_unit", "")
    else:
        count = int(values.get("interval_count") or "1")
        unit = rhythm
    if not 1 <= count <= 9999:
        raise ValueError("Der Rhythmus enthält keine gültige Anzahl.")
    if unit == "day":
        result = current + timedelta(days=count)
    elif unit == "week":
        result = current + timedelta(weeks=count)
    elif unit in {"month", "quarter", "year", "month_start", "month_end"}:
        multiplier = {"quarter": 3, "year": 12}.get(unit, 1)
        start = date.fromisoformat(values["start_date"])
        result = _add_months(current, count * multiplier, anchor_day=start.day)
        if unit == "month_start":
            result = result.replace(day=1)
        elif unit == "month_end":
            result = result.replace(day=monthrange(result.year, result.month)[1])
    else:
        raise ValueError("Der Rhythmus der periodischen Rechnung ist ungültig.")
    if result <= current:
        raise ValueError("Der nächste Rechnungstermin muss nach dem aktuellen Termin liegen.")
    return result


def _due_date(issued_on: date, values: dict[str, str]) -> tuple[date, str]:
    count_text = values.get("payment_due_count") or ""
    unit = values.get("payment_due_unit") or ""
    if count_text and unit:
        count = int(count_text)
    else:
        terms = values.get("payment_terms") or ""
        if terms == "due_on_receipt":
            count, unit = 0, "day"
        elif terms.endswith("_days") and terms.removesuffix("_days").isdigit():
            count, unit = int(terms.removesuffix("_days")), "day"
        else:
            count, unit = 0, "day"
    if count < 0 or count > 9999:
        raise ValueError("Das Zahlungsziel ist ungültig.")
    if unit == "day":
        due = issued_on + timedelta(days=count)
    elif unit == "week":
        due = issued_on + timedelta(weeks=count)
    elif unit == "year":
        due = _add_months(issued_on, count * 12, anchor_day=issued_on.day)
    else:
        raise ValueError("Die Zeiteinheit des Zahlungsziels ist ungültig.")
    terms = "due_on_receipt" if count == 0 and unit == "day" else f"{count}_days" if unit == "day" and count in {7, 14, 30} else ""
    return due, terms


class HubRecurringInvoiceGenerationService:
    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def backfill_missing_cursors(self) -> int:
        rows = self.db.scalars(
            select(HubFinanceRecurringInvoice).where(HubFinanceRecurringInvoice.hub_next_run_on.is_(None))
        ).all()
        count = 0
        for recurring in rows:
            values = self._values(recurring.encrypted_fields_json)
            raw_date = values.get("next_invoice_date") or ""
            if raw_date:
                recurring.hub_next_run_on = date.fromisoformat(raw_date)
                count += 1
        self.db.commit()
        return count

    def create_due(self, *, today: date, limit: int = 25) -> RecurringInvoiceRunResult:
        candidate_ids = self.db.scalars(
            select(HubFinanceRecurringInvoice.id)
            .where(HubFinanceRecurringInvoice.hub_next_run_on <= today)
            .order_by(HubFinanceRecurringInvoice.hub_next_run_on, HubFinanceRecurringInvoice.id)
        ).all()
        created_ids: list[int] = []
        failed_ids: list[int] = []
        existing_count = 0
        for recurring_id in candidate_ids:
            if len(created_ids) >= limit:
                break
            try:
                outcome = self._create_one(recurring_id=recurring_id, today=today)
                if outcome is not None:
                    invoice_id, was_created = outcome
                    if was_created:
                        created_ids.append(invoice_id)
                    else:
                        existing_count += 1
            except Exception:
                self.db.rollback()
                failed_ids.append(recurring_id)
                logger.exception("Could not generate invoice for recurring invoice %s.", recurring_id)
        return RecurringInvoiceRunResult(tuple(created_ids), tuple(failed_ids), existing_count)

    def _create_one(self, *, recurring_id: int, today: date) -> tuple[int, bool] | None:
        recurring = self.db.scalar(
            select(HubFinanceRecurringInvoice)
            .options(selectinload(HubFinanceRecurringInvoice.lines))
            .where(HubFinanceRecurringInvoice.id == recurring_id)
            .with_for_update()
        )
        if recurring is None or recurring.hub_next_run_on is None or recurring.hub_next_run_on > today:
            self.db.rollback()
            return None
        values = self._values(recurring.encrypted_fields_json)
        if values.get("status") != "active":
            self.db.rollback()
            return None
        scheduled_on = recurring.hub_next_run_on
        start_date = date.fromisoformat(values["start_date"])
        end_date = date.fromisoformat(values["end_date"]) if values.get("end_date") else None
        if end_date is not None and scheduled_on > end_date:
            values["status"] = "ended"
            recurring.encrypted_fields_json = self._encrypt(values)
            self.db.commit()
            return None
        if scheduled_on < start_date:
            raise ValueError("Der nächste Rechnungstermin liegt vor dem Startdatum.")
        next_on = next_recurring_date(scheduled_on, values)
        existing = self.db.scalar(select(HubFinanceInvoice).where(
            HubFinanceInvoice.recurring_invoice_id == recurring.id,
            HubFinanceInvoice.recurring_scheduled_on == scheduled_on,
        ))
        was_created = existing is None
        if was_created:
            if not recurring.lines:
                raise ValueError("Die periodische Rechnung hat keine Positionen.")
            due_on, terms = _due_date(scheduled_on, values)
            submitted = {
                "document_field__status": "draft",
                "document_field__invoice_date": scheduled_on.isoformat(),
                "document_field__due_date": due_on.isoformat(),
                "document_field__currency": values.get("currency") or "EUR",
                "document_field__payment_terms": terms,
            }
            for index, line in enumerate(recurring.lines):
                line_values = self._values(line.encrypted_fields_json)
                submitted[f"document_line__{index}__article_id"] = str(line.article_id or "")
                for key in ("name", "sku", "description", "quantity", "unit", "unit_price", "discount_percent", "tax_rate"):
                    submitted[f"document_line__{index}__{key}"] = line_values.get(key, "")
            invoice = HubFinanceDocumentService(db=self.db, cipher=self.cipher).create_document(
                module=INVOICE_MODULE,
                customer_id=recurring.customer_id,
                contact_id=recurring.contact_id,
                link_id=None,
                submitted_values=submitted,
                pdf_template_id=recurring.pdf_template_id,
            )
            invoice.recurring_invoice_id = recurring.id
            invoice.recurring_scheduled_on = scheduled_on
            self.db.flush()
            HubFinancePdfService(db=self.db, cipher=self.cipher).queue(document_type="invoices", document_id=invoice.id)
            write_audit_log(
                self.db,
                site=None,
                actor="system",
                source="hub-recurring-invoices",
                action="create-finance-invoices",
                result="ok",
                detail=f"Created invoice {invoice.id} from recurring invoice {recurring.id} for {scheduled_on.isoformat()}.",
            )
            invoice_id = invoice.id
        else:
            invoice_id = existing.id
        recurring.hub_next_run_on = next_on
        values["next_invoice_date"] = next_on.isoformat()
        if end_date is not None and next_on > end_date:
            values["status"] = "ended"
        recurring.encrypted_fields_json = self._encrypt(values)
        self.db.commit()
        return invoice_id, was_created

    def _values(self, encrypted: str) -> dict[str, str]:
        values = json.loads(self.cipher.decrypt(encrypted))
        if not isinstance(values, dict):
            raise ValueError("Ungültige periodische Rechnungsdaten.")
        return {str(key): str(value) if value is not None else "" for key, value in values.items()}

    def _encrypt(self, values: dict[str, str]) -> str:
        return self.cipher.encrypt(json.dumps(values, ensure_ascii=False, separators=(",", ":")))


def backfill_recurring_invoice_cursors() -> int:
    with SessionLocal() as db:
        return HubRecurringInvoiceGenerationService(db=db, cipher=get_secret_cipher()).backfill_missing_cursors()


def create_due_recurring_invoices(*, limit: int = 25) -> RecurringInvoiceRunResult:
    from datetime import datetime

    with SessionLocal() as db:
        return HubRecurringInvoiceGenerationService(db=db, cipher=get_secret_cipher()).create_due(
            today=datetime.now(_BERLIN).date(), limit=limit
        )
