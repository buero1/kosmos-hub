"""Background import combining Zoho Books sales orders with custom CRM orders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from threading import Lock
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_documents import HubFinanceOrder, HubFinanceOrderLine
from app.models.hub_finance_offer import HubFinanceOffer
from app.models.zoho_books_order_import import ZohoBooksOrderImport, ZohoBooksOrderImportItem
from app.services.zoho_books import ZohoBooksError, ZohoBooksService
from app.services.zoho_crm import ZohoCrmError, ZohoCrmService


_ACTIVE_STATUSES = ("pending", "running")
_CENT = Decimal("0.01")
_QUANTITY_STEP = Decimal("0.01")
_import_start_lock = Lock()


class _BooksOrderReader(Protocol):
    def get_status(self): ...

    def list_all_sales_orders(self) -> tuple[dict[str, object], ...]: ...

    def get_sales_order(self, *, sales_order_id: str) -> dict[str, object]: ...

    def get_books_contact(self, *, contact_id: str) -> dict[str, object]: ...


class _CrmOrderReader(Protocol):
    def get_status(self): ...

    def list_order_records_for_account(self, account_id: str) -> list[dict[str, object]]: ...


@dataclass(frozen=True)
class ZohoBooksOrderImportStatus:
    id: int
    status: str
    total_orders: int
    processed_orders: int
    imported_orders: int
    updated_orders: int
    crm_matched_orders: int
    crm_unmatched_orders: int
    crm_ambiguous_orders: int
    failed_orders: int
    consecutive_failures: int
    cancel_requested: bool
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None


class ZohoBooksOrderImportService:
    """Import Books positions and enrich them from one unambiguous CRM order."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        books_service: _BooksOrderReader | None = None,
        crm_service: _CrmOrderReader | None = None,
    ) -> None:
        self.db = db
        self.cipher = cipher
        settings = get_settings()
        self.books = books_service or ZohoBooksService(
            db=db,
            cipher=cipher,
            public_base_url=settings.public_base_url,
        )
        self.crm = crm_service or ZohoCrmService(
            db=db,
            cipher=cipher,
            public_base_url=settings.public_base_url,
        )

    def status(self) -> ZohoBooksOrderImportStatus | None:
        run = self._active_import()
        if run is None:
            run = self.db.scalar(
                select(ZohoBooksOrderImport).order_by(ZohoBooksOrderImport.id.desc()).limit(1)
            )
        return self._status(run) if run is not None else None

    def start_all(self, *, requested_by: str) -> tuple[ZohoBooksOrderImportStatus, bool]:
        with _import_start_lock:
            active = self._active_import()
            if active is not None:
                return self._status(active), False
            books_status = self.books.get_status()
            organization_id = self._text(getattr(books_status, "organization_id", ""))
            if not organization_id:
                raise ZohoBooksError("Wähle zuerst die Zoho-Books-Organisation für den Import aus.")
            crm_status = self.crm.get_status()
            if not bool(getattr(crm_status, "connected", False)):
                raise ZohoCrmError("Verbinde zuerst Zoho CRM für den kombinierten Auftragsimport.")
            source_rows = self.books.list_all_sales_orders()
            if not source_rows:
                raise ZohoBooksError("Zoho Books enthält keine Aufträge für den Import.")
            run = ZohoBooksOrderImport(
                requested_by=requested_by[:128],
                organization_id=organization_id,
                status="pending",
                total_orders=len(source_rows),
            )
            self.db.add(run)
            self.db.flush()
            self.db.add_all(
                ZohoBooksOrderImportItem(
                    order_import_id=run.id,
                    zoho_salesorder_id=self._text(row.get("salesorder_id")),
                    zoho_books_customer_id=self._text(row.get("customer_id")) or None,
                )
                for row in source_rows
            )
            self.db.flush()
            return self._status(run), True

    def cancel(self) -> tuple[ZohoBooksOrderImportStatus | None, bool]:
        run = self._active_import()
        if run is None:
            return self.status(), False
        run.cancel_requested = True
        self.db.flush()
        return self._status(run), True

    def process_next_order(self) -> str | None:
        """Import one order without holding a transaction during remote API calls."""
        run = self._active_import()
        if run is None:
            return None
        if run.cancel_requested:
            run.status = "cancelled"
            run.completed_at = datetime.now(UTC)
            run.last_error = "Der Auftragsimport wurde durch den Benutzer abgebrochen."
            self.db.commit()
            return "cancelled"
        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC)

        item = self.db.scalar(
            select(ZohoBooksOrderImportItem)
            .where(
                ZohoBooksOrderImportItem.order_import_id == run.id,
                ZohoBooksOrderImportItem.status == "pending",
            )
            .order_by(ZohoBooksOrderImportItem.id.asc())
            .limit(1)
        )
        if item is None:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            self.db.commit()
            return "completed"

        run_id = run.id
        item_id = item.id
        source_id = item.zoho_salesorder_id
        self.db.commit()
        try:
            books_payload = self.books.get_sales_order(sales_order_id=source_id)
            customer, books_contact, crm_account_id = self._resolve_customer(books_payload)
            crm_records = (
                self.crm.list_order_records_for_account(crm_account_id)
                if crm_account_id
                else []
            )
            crm_payload, crm_match_status = self._matching_crm_order(
                records=crm_records,
                books_payload=books_payload,
                books_customer_is_unique=self._books_customer_is_unique(item),
            )
            if crm_payload is not None and self._crm_order_used_elsewhere(
                crm_order_id=self._record_id(crm_payload),
                books_order_id=source_id,
            ):
                crm_payload = None
                crm_match_status = "ambiguous"
            order, was_created = self._upsert_order(
                books_payload=books_payload,
                crm_payload=crm_payload,
                customer=customer,
                books_contact=books_contact,
                source_id=source_id,
            )
        except (ZohoBooksError, ZohoCrmError, ValueError) as exc:
            self.db.rollback()
            stopped = self._record_failure(run_id=run_id, item_id=item_id, error=str(exc))
            return "stopped" if stopped else "failed"
        except Exception:
            self.db.rollback()
            stopped = self._record_failure(
                run_id=run_id,
                item_id=item_id,
                error="Der Auftrag konnte nicht vollständig aus Zoho importiert werden.",
            )
            return "stopped" if stopped else "failed"

        run = self.db.get(ZohoBooksOrderImport, run_id)
        item = self.db.get(ZohoBooksOrderImportItem, item_id)
        if run is None or item is None:
            self.db.rollback()
            return None
        item.local_order_id = order.id
        item.zoho_crm_order_id = order.zoho_crm_id if crm_match_status == "matched" else None
        item.crm_match_status = crm_match_status
        item.status = "imported"
        item.last_error = None
        run.processed_orders += 1
        if was_created:
            run.imported_orders += 1
        else:
            run.updated_orders += 1
        if crm_match_status == "matched":
            run.crm_matched_orders += 1
        elif crm_match_status == "ambiguous":
            run.crm_ambiguous_orders += 1
        else:
            run.crm_unmatched_orders += 1
        run.consecutive_failures = 0
        run.last_error = None
        self.db.commit()
        return "succeeded"

    def _resolve_customer(
        self,
        books_payload: dict[str, object],
    ) -> tuple[Customer | None, dict[str, object] | None, str]:
        crm_account_id = self._text(
            books_payload.get("zcrm_account_id") or books_payload.get("crm_account_id")
        )
        books_contact: dict[str, object] | None = None
        books_customer_id = self._text(books_payload.get("customer_id"))
        if books_customer_id:
            books_contact = self.books.get_books_contact(contact_id=books_customer_id)
            crm_account_id = crm_account_id or self._text(
                books_contact.get("zcrm_account_id") or books_contact.get("crm_account_id")
            )
        customer = self.db.scalar(select(Customer).where(Customer.zoho_id == crm_account_id)) if crm_account_id else None
        return customer, books_contact, crm_account_id

    def _matching_crm_order(
        self,
        *,
        records: list[dict[str, object]],
        books_payload: dict[str, object],
        books_customer_is_unique: bool,
    ) -> tuple[dict[str, object] | None, str]:
        records = [record for record in records if self._record_id(record)]
        if not records:
            return None, "unmatched"
        books_date = self._date(books_payload.get("date"))
        dated = [(record, self._date(record.get("Vertragsdatum"))) for record in records]
        if books_date is not None:
            exact = [record for record, crm_date in dated if crm_date == books_date]
            if len(exact) == 1:
                return exact[0], "matched"
            close = [
                (record, abs((crm_date - books_date).days))
                for record, crm_date in dated
                if crm_date is not None and abs((crm_date - books_date).days) <= 1
            ]
            if close:
                nearest_distance = min(distance for _, distance in close)
                nearest = [record for record, distance in close if distance == nearest_distance]
                if len(nearest) == 1:
                    return nearest[0], "matched"
        if len(records) == 1 and books_customer_is_unique:
            return records[0], "matched"
        return None, "ambiguous"

    def _books_customer_is_unique(self, item: ZohoBooksOrderImportItem) -> bool:
        customer_id = self._text(item.zoho_books_customer_id)
        if not customer_id:
            return False
        count = self.db.scalar(
            select(func.count())
            .select_from(ZohoBooksOrderImportItem)
            .where(
                ZohoBooksOrderImportItem.order_import_id == item.order_import_id,
                ZohoBooksOrderImportItem.zoho_books_customer_id == customer_id,
            )
        )
        return count == 1

    def _crm_order_used_elsewhere(self, *, crm_order_id: str, books_order_id: str) -> bool:
        if not crm_order_id:
            return False
        order = self.db.scalar(select(HubFinanceOrder).where(HubFinanceOrder.zoho_crm_id == crm_order_id))
        return order is not None and order.zoho_books_id != books_order_id

    def _upsert_order(
        self,
        *,
        books_payload: dict[str, object],
        crm_payload: dict[str, object] | None,
        customer: Customer | None,
        books_contact: dict[str, object] | None,
        source_id: str,
    ) -> tuple[HubFinanceOrder, bool]:
        returned_source_id = self._text(books_payload.get("salesorder_id"))
        if returned_source_id and returned_source_id != source_id:
            raise ZohoBooksError("Zoho Books hat einen nicht passenden Auftrag zurückgegeben.")
        order = self.db.scalar(select(HubFinanceOrder).where(HubFinanceOrder.zoho_books_id == source_id))
        was_created = order is None
        if order is None:
            order = HubFinanceOrder(zoho_books_id=source_id, encrypted_fields_json=self._encrypt({}))
            self.db.add(order)
            self.db.flush()

        current_values = self._decrypt(order.encrypted_fields_json)
        values = self._books_values(books_payload=books_payload, current_values=current_values)
        if crm_payload is not None:
            values.update(self._crm_values(crm_payload))
            order.zoho_crm_id = self._record_id(crm_payload)
        values["order_name"] = self._normalized_order_name(
            values.get("order_name", ""),
            order_number=self._text(books_payload.get("salesorder_number")),
        )
        order.customer = customer
        order.contact = self._matching_contact(
            crm_payload=crm_payload,
            books_payload=books_payload,
            books_contact=books_contact,
            customer=customer,
        )
        order.offer = self._matching_offer(books_payload)
        order.order_number = self._available_order_number(
            order=order,
            number=self._text(books_payload.get("salesorder_number")) or f"Zoho-{source_id}",
            source_id=source_id,
        )
        order.encrypted_fields_json = self._encrypt(values)
        order.zoho_modified_at = self._latest_timestamp(
            books_payload.get("last_modified_time"),
            crm_payload.get("Modified_Time") if crm_payload else None,
        )
        order.zoho_imported_at = datetime.now(UTC)
        self._replace_lines(order=order, payload=books_payload)
        self.db.flush()
        return order, was_created

    def _books_values(
        self,
        *,
        books_payload: dict[str, object],
        current_values: dict[str, str],
    ) -> dict[str, str]:
        values = dict(current_values)
        values.update(
            {
                "order_name": values.get("order_name") or self._text(books_payload.get("salesorder_number")),
                "status": self._status_value(books_payload.get("status") or books_payload.get("order_status")),
                "order_date": values.get("order_date") or self._date_text(books_payload.get("date")),
                "created_time": values.get("created_time") or self._text(books_payload.get("created_time")),
                "modified_time": values.get("modified_time") or self._text(books_payload.get("last_modified_time")),
            }
        )
        return values

    def _crm_values(self, payload: dict[str, object]) -> dict[str, str]:
        mapping = {
            "order_name": "Name",
            "created_time": "Created_Time",
            "modified_time": "Modified_Time",
            "outdoor_sales": "Aussendienst",
            "contract_start": "Vertragsbeginn",
            "contract_note": "Vertragsbemerkung",
            "order_date": "Vertragsdatum",
            "contract_term": "Vertragslaufzeit",
            "payment_method": "Zahlungsart",
            "payment_frequency": "Zahlweise",
            "cancellation_period": "K_ndigungsfrist",
            "domain_request": "Domainwunsch",
            "order_intake_type": "Auftragsaufnahme_Art",
            "contract_duration_years": "Vertragsdauer_in_Jahren",
            "cancellation_date": "K_ndigungsdatum",
            "order_won_by": "Auftrag_erzielt_von",
            "agent_order_rating": "Auftragswertung_Agent",
        }
        return {key: self._text(payload.get(api_name)) for key, api_name in mapping.items()}

    @staticmethod
    def _normalized_order_name(name: str, *, order_number: str) -> str:
        normalized_name = name.strip()
        if normalized_name.casefold() == "auftrag":
            return "Website"
        if order_number.strip() and normalized_name.casefold() == order_number.strip().casefold():
            return "Website"
        return name

    def _matching_contact(
        self,
        *,
        crm_payload: dict[str, object] | None,
        books_payload: dict[str, object],
        books_contact: dict[str, object] | None,
        customer: Customer | None,
    ) -> CustomerContact | None:
        source_ids: list[str] = []
        crm_lookup = crm_payload.get("Kontakt") if isinstance(crm_payload, dict) else None
        if isinstance(crm_lookup, dict):
            source_ids.append(self._text(crm_lookup.get("id")))
        source_ids.extend(
            self._text(books_payload.get(key))
            for key in ("zcrm_contact_id", "contact_person_id", "contact_id")
        )
        associated = books_payload.get("contact_persons_associated")
        if isinstance(associated, list):
            books_person_ids = {
                self._text(person.get("contact_person_id"))
                for person in associated
                if isinstance(person, dict)
            }
            people = books_contact.get("contact_persons") if isinstance(books_contact, dict) else None
            if isinstance(people, list):
                source_ids.extend(
                    self._text(person.get("zcrm_contact_id"))
                    for person in people
                    if isinstance(person, dict)
                    and self._text(person.get("contact_person_id")) in books_person_ids
                )
        for source_id in source_ids:
            if not source_id:
                continue
            contact = self.db.scalar(select(CustomerContact).where(CustomerContact.zoho_id == source_id))
            if contact is not None and (customer is None or contact.customer_id == customer.id):
                return contact
        return None

    def _matching_offer(self, payload: dict[str, object]) -> HubFinanceOffer | None:
        offer_id = self._text(payload.get("estimate_id"))
        if offer_id:
            offer = self.db.scalar(select(HubFinanceOffer).where(HubFinanceOffer.zoho_books_id == offer_id))
            if offer is not None:
                return offer
        reference = self._text(payload.get("reference_number"))
        return self.db.scalar(select(HubFinanceOffer).where(HubFinanceOffer.offer_number == reference)) if reference else None

    def _replace_lines(self, *, order: HubFinanceOrder, payload: dict[str, object]) -> None:
        for line in tuple(order.lines):
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
            order.lines.append(
                HubFinanceOrderLine(
                    article=article,
                    position_index=index,
                    encrypted_fields_json=self._encrypt(values),
                )
            )

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

    def _available_order_number(self, *, order: HubFinanceOrder, number: str, source_id: str) -> str:
        existing = self.db.scalar(select(HubFinanceOrder).where(HubFinanceOrder.order_number == number))
        if existing is None or existing.id == order.id:
            return number[:255]
        return f"{number[:220]} (Zoho {source_id[-24:]})"

    def _record_failure(self, *, run_id: int, item_id: int, error: str) -> bool:
        run = self.db.get(ZohoBooksOrderImport, run_id)
        item = self.db.get(ZohoBooksOrderImportItem, item_id)
        if run is None or item is None:
            self.db.rollback()
            return False
        message = self._safe_message(error)
        item.status = "failed"
        item.last_error = message
        run.processed_orders += 1
        run.failed_orders += 1
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

    def _active_import(self) -> ZohoBooksOrderImport | None:
        return self.db.scalar(
            select(ZohoBooksOrderImport)
            .where(ZohoBooksOrderImport.status.in_(_ACTIVE_STATUSES))
            .order_by(ZohoBooksOrderImport.id.asc())
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
    def _record_id(record: dict[str, object]) -> str:
        return ZohoBooksOrderImportService._text(record.get("id"))

    @staticmethod
    def _status_value(value: object) -> str:
        status = ZohoBooksOrderImportService._text(value).casefold()
        if status in {"void", "voided", "cancelled", "canceled"}:
            return "cancelled"
        if status in {"fulfilled", "closed", "completed", "invoiced"}:
            return "completed"
        if status in {"confirmed", "open", "approved", "overdue"}:
            return "confirmed"
        return "draft"

    @staticmethod
    def _free_text_values(*, article: HubFinanceArticle | None, name: str, description: str) -> tuple[str, str]:
        if (
            article is None
            and description.strip()
            and (not name.strip() or name.strip().casefold() in {"freitextposition", "freitext position"})
        ):
            return description, ""
        return name, description

    @staticmethod
    def _discount_percent(raw_line: dict[str, object]) -> str:
        raw = ZohoBooksOrderImportService._text(raw_line.get("discount")).strip()
        if raw.endswith("%"):
            raw = raw[:-1].strip()
        value = ZohoBooksOrderImportService._decimal_text(raw, default="0", places=_CENT)
        try:
            return value if Decimal(value) <= Decimal("100") else "0.00"
        except InvalidOperation:
            return "0.00"

    @staticmethod
    def _tax_rate(value: object) -> str:
        decimal_value = ZohoBooksOrderImportService._decimal_text(value, default="19", places=_CENT)
        if decimal_value in {"0.00", "0"}:
            return "0"
        if decimal_value in {"7.00", "7"}:
            return "7"
        return "19"

    @staticmethod
    def _decimal_text(value: object, *, default: str, places: Decimal) -> str:
        raw = ZohoBooksOrderImportService._text(value).replace(",", ".").strip() or default
        try:
            decimal_value = Decimal(raw).quantize(places, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            decimal_value = Decimal(default).quantize(places, rounding=ROUND_HALF_UP)
        return format(decimal_value, "f")

    @staticmethod
    def _date(value: object) -> date | None:
        text = ZohoBooksOrderImportService._text(value)
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return None

    @staticmethod
    def _date_text(value: object) -> str:
        parsed = ZohoBooksOrderImportService._date(value)
        return parsed.isoformat() if parsed is not None else ""

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        text = ZohoBooksOrderImportService._text(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @classmethod
    def _latest_timestamp(cls, *values: object) -> datetime | None:
        timestamps = [timestamp for value in values if (timestamp := cls._timestamp(value)) is not None]
        return max(timestamps) if timestamps else None

    @staticmethod
    def _text(value: object) -> str:
        return str(value).strip() if isinstance(value, (str, int, float, Decimal)) else ""

    @staticmethod
    def _safe_message(error: str) -> str:
        if "Zoho rejected the request" in error or error.startswith("Zoho Books") or error.startswith("Zoho CRM"):
            return error.splitlines()[0][:1_000]
        return "Der Auftrag konnte nicht vollständig aus Zoho importiert werden."

    @staticmethod
    def _status(run: ZohoBooksOrderImport) -> ZohoBooksOrderImportStatus:
        return ZohoBooksOrderImportStatus(
            id=run.id,
            status=run.status,
            total_orders=run.total_orders,
            processed_orders=run.processed_orders,
            imported_orders=run.imported_orders,
            updated_orders=run.updated_orders,
            crm_matched_orders=run.crm_matched_orders,
            crm_unmatched_orders=run.crm_unmatched_orders,
            crm_ambiguous_orders=run.crm_ambiguous_orders,
            failed_orders=run.failed_orders,
            consecutive_failures=run.consecutive_failures,
            cancel_requested=run.cancel_requested,
            started_at=run.started_at,
            completed_at=run.completed_at,
            last_error=run.last_error,
        )
