"""Shared local persistence for Finance orders, invoices and recurring invoices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_documents import (
    HubFinanceInvoice,
    HubFinanceInvoiceLine,
    HubFinanceOrder,
    HubFinanceOrderLine,
    HubFinanceRecurringInvoice,
    HubFinanceRecurringInvoiceLine,
)
from app.models.hub_finance_offer import HubFinanceOffer
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_finance import (
    FinanceArticleEntry,
    FinanceContactOption,
    FinanceOfferLineView,
    FinanceOfferTotals,
    HubFinanceService,
)
from app.services.hub_finance_document_field_catalog import INVOICE_FIELDS, ORDER_FIELDS, RECURRING_INVOICE_FIELDS
from app.services.hub_finance_field_catalog import HubFinanceField
from app.services.module_layouts import ModuleLayoutService


ORDER_FIELDS_LAYOUT_KEY = "finance-order-fields"
INVOICE_FIELDS_LAYOUT_KEY = "finance-invoice-fields"
RECURRING_INVOICE_FIELDS_LAYOUT_KEY = "finance-recurring-invoice-fields"
_CENT = Decimal("0.01")
_QUANTITY_STEP = Decimal("0.001")


class HubFinanceDocumentError(ValueError):
    """A safe validation message for the remaining Finance modules."""


@dataclass(frozen=True)
class FinanceDocumentModule:
    key: str
    label: str
    singular: str
    eyebrow: str
    fields: tuple[HubFinanceField, ...]
    layout_key: str
    model: type[Any]
    line_model: type[Any]
    number_attribute: str | None
    number_prefix: str | None
    date_key: str
    link_key: str | None = None
    link_attribute: str | None = None
    link_model: type[Any] | None = None
    is_invoice: bool = False
    is_recurring: bool = False


ORDER_MODULE = FinanceDocumentModule(
    key="orders",
    label="Aufträge",
    singular="Auftrag",
    eyebrow="Finance · Auftrag",
    fields=ORDER_FIELDS,
    layout_key=ORDER_FIELDS_LAYOUT_KEY,
    model=HubFinanceOrder,
    line_model=HubFinanceOrderLine,
    number_attribute="order_number",
    number_prefix="AUF",
    date_key="order_date",
    link_key="linked_offer",
    link_attribute="offer",
    link_model=HubFinanceOffer,
)

INVOICE_MODULE = FinanceDocumentModule(
    key="invoices",
    label="Rechnungen",
    singular="Rechnung",
    eyebrow="Finance · Rechnung",
    fields=INVOICE_FIELDS,
    layout_key=INVOICE_FIELDS_LAYOUT_KEY,
    model=HubFinanceInvoice,
    line_model=HubFinanceInvoiceLine,
    number_attribute="invoice_number",
    number_prefix="RE",
    date_key="invoice_date",
    link_key="linked_order",
    link_attribute="order",
    link_model=HubFinanceOrder,
    is_invoice=True,
)

RECURRING_INVOICE_MODULE = FinanceDocumentModule(
    key="recurring-invoices",
    label="Periodische Rechnungen",
    singular="Periodische Rechnung",
    eyebrow="Finance · Periodische Rechnung",
    fields=RECURRING_INVOICE_FIELDS,
    layout_key=RECURRING_INVOICE_FIELDS_LAYOUT_KEY,
    model=HubFinanceRecurringInvoice,
    line_model=HubFinanceRecurringInvoiceLine,
    number_attribute=None,
    number_prefix=None,
    date_key="next_invoice_date",
    is_recurring=True,
)

FINANCE_DOCUMENT_MODULES = {
    module.key: module
    for module in (ORDER_MODULE, INVOICE_MODULE, RECURRING_INVOICE_MODULE)
}


@dataclass(frozen=True)
class FinanceDocumentFieldValue:
    key: str
    label: str
    display_type: str
    value: str
    form_value: str
    required: bool = False
    read_only: bool = False
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FinanceDocumentLinkOption:
    id: int
    label: str
    customer_id: int | None


@dataclass(frozen=True)
class FinanceDocumentEntry:
    document: Any
    identifier: str
    status: str
    customer_name: str
    document_date: str
    total_gross: str


@dataclass(frozen=True)
class FinanceDocumentDetail:
    document: Any
    module: FinanceDocumentModule
    identifier: str
    status: str
    contact_name: str
    link_label: str
    fields: tuple[FinanceDocumentFieldValue, ...]
    lines: tuple[FinanceOfferLineView, ...]
    totals: FinanceOfferTotals
    billing_address: str = ""


class HubFinanceDocumentService:
    """Keep Finance document modules uniform before the later Books import."""

    _MAX_TEXT_LENGTH = 20_000

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    @staticmethod
    def module(key: str) -> FinanceDocumentModule:
        module = FINANCE_DOCUMENT_MODULES.get(key)
        if module is None:
            raise HubFinanceDocumentError("Das Finance-Modul wurde nicht gefunden.")
        return module

    def list_documents(self, *, module: FinanceDocumentModule) -> tuple[FinanceDocumentEntry, ...]:
        documents = self.db.scalars(
            select(module.model).options(selectinload(module.model.customer), selectinload(module.model.lines))
        ).all()
        entries = [self._entry(module=module, document=document) for document in documents]
        return tuple(sorted(
            entries,
            key=lambda entry: (
                self._text(self._values(entry.document.encrypted_fields_json).get(module.date_key)),
                entry.document.created_at.isoformat() if entry.document.created_at else "",
                entry.document.id,
            ),
            reverse=True,
        ))

    def get_detail(self, *, module: FinanceDocumentModule, document_id: int) -> FinanceDocumentDetail | None:
        document = self.db.scalar(
            select(module.model)
            .options(
                selectinload(module.model.customer),
                selectinload(module.model.contact),
                selectinload(module.model.lines).selectinload(module.line_model.article),
            )
            .where(module.model.id == document_id)
        )
        if document is None:
            return None
        if module.link_attribute:
            document = self.db.scalar(
                select(module.model)
                .options(
                    selectinload(module.model.customer),
                    selectinload(module.model.contact),
                    selectinload(module.model.lines).selectinload(module.line_model.article),
                    selectinload(getattr(module.model, module.link_attribute)),
                )
                .where(module.model.id == document_id)
            )
        assert document is not None
        values = self._values(document.encrypted_fields_json)
        lines = tuple(self._line_view(line) for line in document.lines)
        totals = self._totals(lines)
        contact_name = self._contact_name(document.contact_id)
        identifier = self.identifier(module=module, document=document, values=values)
        link_label = self._link_label(module=module, document=document)
        enriched_values = {
            **values,
            "customer": document.customer.name if document.customer is not None else "",
            "contact": contact_name,
        }
        if module.number_attribute:
            enriched_values[next(field.key for field in module.fields if field.display_type == "Autonummer")] = identifier
        if module.link_key:
            enriched_values[module.link_key] = link_label
        if module.is_invoice:
            enriched_values["remaining_amount"] = self._decimal_string(self._remaining_amount(values=values, totals=totals))
        fields = self._field_values(module=module, values=enriched_values)
        return FinanceDocumentDetail(
            document=document,
            module=module,
            identifier=identifier,
            status=self._display_option(self._field(module.fields, "status"), values.get("status")) or "-",
            contact_name=contact_name,
            link_label=link_label,
            fields=fields,
            lines=lines,
            totals=totals,
            billing_address=self._text(values.get("billing_address")) if module.is_invoice else "",
        )

    def new_form_values(self, *, module: FinanceDocumentModule) -> dict[str, str]:
        today = date.today().isoformat()
        values = {
            "document_field__status": "active" if module.is_recurring else "draft",
            "document_field__currency": "EUR",
            "document_line__0__quantity": "1",
            "document_line__0__tax_rate": "19",
            "document_line__0__discount_percent": "0",
        }
        if module is ORDER_MODULE:
            values["document_field__order_date"] = today
        elif module is INVOICE_MODULE:
            values["document_field__invoice_date"] = today
            values["document_field__due_date"] = today
        else:
            values.update({
                "document_field__start_date": today,
                "document_field__next_invoice_date": today,
                "document_field__interval_unit": "month",
                "document_field__interval_count": "1",
                "document_field__automatic_creation": "true",
                "document_field__late_fee": "0",
            })
        return values

    def create_document(
        self,
        *,
        module: FinanceDocumentModule,
        customer_id: int | None,
        contact_id: int | None,
        link_id: int | None,
        submitted_values: dict[str, str],
    ) -> Any:
        customer, contact = self._customer_and_contact(customer_id=customer_id, contact_id=contact_id)
        linked_record = self._linked_record(module=module, link_id=link_id, customer_id=customer.id)
        values = self._submitted_fields(module=module, submitted_values=submitted_values)
        if module.is_invoice:
            values["billing_address"] = self._billing_address(customer)
        constructor: dict[str, Any] = {"customer": customer, "contact": contact, "encrypted_fields_json": self._encrypt(values)}
        if module.link_attribute:
            constructor[module.link_attribute] = linked_record
        document = module.model(**constructor)
        self.db.add(document)
        self.db.flush()
        if module.number_attribute and module.number_prefix:
            setattr(document, module.number_attribute, f"{module.number_prefix}-{document.id:06d}")
        self._replace_lines(module=module, document=document, submitted_values=submitted_values)
        self.db.flush()
        return document

    def update_document(
        self,
        *,
        module: FinanceDocumentModule,
        document_id: int,
        customer_id: int | None,
        contact_id: int | None,
        link_id: int | None,
        submitted_values: dict[str, str],
    ) -> Any:
        document = self.db.scalar(select(module.model).options(selectinload(module.model.lines)).where(module.model.id == document_id))
        if document is None:
            raise HubFinanceDocumentError(f"{module.singular} wurde nicht gefunden.")
        customer, contact = self._customer_and_contact(customer_id=customer_id, contact_id=contact_id)
        linked_record = self._linked_record(module=module, link_id=link_id, customer_id=customer.id)
        existing_values = self._values(document.encrypted_fields_json)
        values = self._submitted_fields(module=module, submitted_values=submitted_values)
        if module.is_invoice:
            values["billing_address"] = existing_values.get("billing_address") or self._billing_address(customer)
        document.customer = customer
        document.contact = contact
        if module.link_attribute:
            setattr(document, module.link_attribute, linked_record)
        document.encrypted_fields_json = self._encrypt(values)
        self._replace_lines(module=module, document=document, submitted_values=submitted_values)
        self.db.flush()
        return document

    def delete_document(self, *, module: FinanceDocumentModule, document_id: int) -> Any:
        document = self.db.get(module.model, document_id)
        if document is None:
            raise HubFinanceDocumentError(f"{module.singular} wurde nicht gefunden.")
        self.db.delete(document)
        self.db.flush()
        return document

    def list_linkable_customers(self) -> tuple[Customer, ...]:
        return tuple(self.db.scalars(select(Customer).where(Customer.is_visible.is_(True)).order_by(Customer.name.asc())).all())

    def list_linkable_contacts(self) -> tuple[FinanceContactOption, ...]:
        entries = CustomerDirectoryService(db=self.db, cipher=self.cipher).list_contact_entries()
        return tuple(
            FinanceContactOption(id=entry.contact.id, name=entry.contact.name, customer_id=entry.customer.id, customer_name=entry.customer.name)
            for entry in entries if entry.customer is not None
        )

    def article_options(self) -> tuple[FinanceArticleEntry, ...]:
        return HubFinanceService(db=self.db, cipher=self.cipher).article_options()

    def link_options(self, *, module: FinanceDocumentModule) -> tuple[FinanceDocumentLinkOption, ...]:
        if module is ORDER_MODULE:
            return tuple(
                FinanceDocumentLinkOption(id=entry.offer.id, label=entry.offer_number, customer_id=entry.offer.customer_id)
                for entry in HubFinanceService(db=self.db, cipher=self.cipher).list_offers()
            )
        if module is INVOICE_MODULE:
            return tuple(
                FinanceDocumentLinkOption(id=entry.document.id, label=entry.identifier, customer_id=entry.document.customer_id)
                for entry in self.list_documents(module=ORDER_MODULE)
            )
        return ()

    def identifier(self, *, module: FinanceDocumentModule, document: Any, values: dict[str, str] | None = None) -> str:
        values = values if values is not None else self._values(document.encrypted_fields_json)
        if module.number_attribute:
            return self._text(getattr(document, module.number_attribute)) or f"{module.number_prefix}-{document.id:06d}"
        return self._text(values.get("name")) or f"Periodische Rechnung {document.id}"

    def _entry(self, *, module: FinanceDocumentModule, document: Any) -> FinanceDocumentEntry:
        values = self._values(document.encrypted_fields_json)
        lines = tuple(self._line_view(line) for line in document.lines)
        currency = self._text(values.get("currency")) or "EUR"
        return FinanceDocumentEntry(
            document=document,
            identifier=self.identifier(module=module, document=document, values=values),
            status=self._display_option(self._field(module.fields, "status"), values.get("status")) or "-",
            customer_name=document.customer.name if document.customer else "-",
            document_date=self._display_date(self._text(values.get(module.date_key))) or "-",
            total_gross=HubFinanceService.format_money(self._totals(lines).total_gross, currency),
        )

    def _field_values(self, *, module: FinanceDocumentModule, values: dict[str, str]) -> tuple[FinanceDocumentFieldValue, ...]:
        ordered_keys = ModuleLayoutService(db=self.db).ordered_keys(
            layout_key=module.layout_key,
            default_keys=tuple(field.key for field in module.fields),
        )
        by_key = {field.key: field for field in module.fields}
        result = []
        for key in ordered_keys:
            definition = by_key[key]
            raw_value = self._text(values.get(key))
            value = raw_value
            if definition.display_type == "Auswahlliste":
                value = self._display_option(definition, raw_value)
            elif definition.display_type == "Waehrung":
                value = HubFinanceService.format_money(self._money(raw_value))
            elif definition.display_type == "Datum":
                value = self._display_date(raw_value)
            elif definition.display_type == "Boolesch":
                value = "Ja" if self._truthy(raw_value) else "Nein"
            result.append(FinanceDocumentFieldValue(
                key=definition.key,
                label=definition.label,
                display_type=definition.display_type,
                value=value,
                form_value=raw_value,
                required=definition.required,
                read_only=definition.read_only,
                options=definition.options,
            ))
        return tuple(result)

    def _submitted_fields(self, *, module: FinanceDocumentModule, submitted_values: dict[str, str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for definition in module.fields:
            if definition.read_only or definition.key in {"customer", "contact", module.link_key}:
                continue
            raw = self._limited_text(submitted_values.get(f"document_field__{definition.key}"), definition.label)
            if definition.display_type == "Boolesch":
                raw = "true" if self._truthy(raw) else "false"
            if definition.required and not raw:
                raise HubFinanceDocumentError(f"{definition.label} ist erforderlich.")
            if definition.display_type == "Datum" and raw:
                self._validate_date(raw, definition.label)
            elif definition.display_type == "Ganzzahl" and raw:
                raw = self._integer_text(raw, definition.label, minimum=1)
            elif definition.display_type == "Waehrung" and raw:
                raw = self._decimal_text(raw, definition.label, minimum=Decimal("0"), places=_CENT)
            self._validate_option(definition, raw)
            values[definition.key] = raw
        if module is INVOICE_MODULE and values.get("due_date", "") < values.get("invoice_date", ""):
            raise HubFinanceDocumentError("Fällig am darf nicht vor dem Rechnungsdatum liegen.")
        if module is RECURRING_INVOICE_MODULE and values.get("end_date") and values["end_date"] < values.get("start_date", ""):
            raise HubFinanceDocumentError("Enddatum darf nicht vor dem Startdatum liegen.")
        return values

    def _replace_lines(self, *, module: FinanceDocumentModule, document: Any, submitted_values: dict[str, str]) -> None:
        for line in tuple(document.lines):
            self.db.delete(line)
        self.db.flush()
        for index, values in enumerate(self._submitted_lines(submitted_values)):
            article_id = self._optional_id(values.pop("article_id", ""), "Artikel")
            article = self.db.get(HubFinanceArticle, article_id) if article_id is not None else None
            if article_id is not None and article is None:
                raise HubFinanceDocumentError("Der ausgewählte Artikel wurde nicht gefunden.")
            document.lines.append(module.line_model(
                article=article,
                position_index=index,
                encrypted_fields_json=self._encrypt(values),
            ))

    def _submitted_lines(self, submitted_values: dict[str, str]) -> tuple[dict[str, str], ...]:
        rows: dict[int, dict[str, str]] = {}
        for key, value in submitted_values.items():
            parts = key.split("__")
            if len(parts) != 3 or parts[0] != "document_line" or not parts[1].isdigit():
                continue
            rows.setdefault(int(parts[1]), {})[parts[2]] = str(value)
        result = []
        for index in sorted(rows):
            row = rows[index]
            if self._truthy(row.get("delete")):
                continue
            article_id = self._limited_text(row.get("article_id"), "Artikel")
            name = self._limited_text(row.get("name"), "Position: Bezeichnung")
            description = self._limited_text(row.get("description"), "Position: Beschreibung")
            if not article_id and not name and not description:
                continue
            if not name:
                raise HubFinanceDocumentError("Jede Position benötigt eine Bezeichnung.")
            quantity = self._decimal_text(row.get("quantity"), "Position: Menge", minimum=Decimal("0.001"), places=_QUANTITY_STEP)
            unit_price = self._decimal_text(row.get("unit_price"), "Position: Einzelpreis netto", minimum=Decimal("0"), places=_CENT)
            discount = self._decimal_text(row.get("discount_percent"), "Position: Rabatt", minimum=Decimal("0"), maximum=Decimal("100"), places=_CENT)
            tax_rate = self._limited_text(row.get("tax_rate"), "Position: MwSt.-Satz")
            if tax_rate not in {"0", "7", "19"}:
                raise HubFinanceDocumentError("Die Auswahl für Position: MwSt.-Satz ist ungültig.")
            result.append({
                "article_id": article_id,
                "name": name,
                "sku": self._limited_text(row.get("sku"), "Artikelnummer / SKU"),
                "description": description,
                "quantity": quantity,
                "unit": self._limited_text(row.get("unit"), "Position: Einheit"),
                "unit_price": unit_price,
                "discount_percent": discount,
                "tax_rate": tax_rate,
            })
        return tuple(result)

    def _customer_and_contact(self, *, customer_id: int | None, contact_id: int | None) -> tuple[Customer, CustomerContact | None]:
        if customer_id is None:
            raise HubFinanceDocumentError("Kunde ist erforderlich.")
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            raise HubFinanceDocumentError("Der ausgewählte Kunde ist nicht verfügbar.")
        if contact_id is None:
            return customer, None
        contact = self.db.get(CustomerContact, contact_id)
        if contact is None or contact.customer_id != customer.id:
            raise HubFinanceDocumentError("Der Ansprechpartner gehört nicht zum ausgewählten Kunden.")
        return customer, contact

    def _linked_record(self, *, module: FinanceDocumentModule, link_id: int | None, customer_id: int) -> Any | None:
        if module.link_model is None:
            return None
        if link_id is None:
            return None
        record = self.db.get(module.link_model, link_id)
        if record is None or record.customer_id != customer_id:
            raise HubFinanceDocumentError("Der verknüpfte Beleg gehört nicht zum ausgewählten Kunden.")
        return record

    def _line_view(self, line: Any) -> FinanceOfferLineView:
        values = self._values(line.encrypted_fields_json)
        quantity = self._quantity(values.get("quantity"))
        unit_price = self._money(values.get("unit_price"))
        discount = self._percentage(values.get("discount_percent"))
        tax_rate = self._percentage(values.get("tax_rate"))
        raw_total = (quantity * unit_price).quantize(_CENT, rounding=ROUND_HALF_UP)
        discount_value = (raw_total * discount / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)
        amount_net = raw_total - discount_value
        tax_amount = (amount_net * tax_rate / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)
        return FinanceOfferLineView(
            id=line.id,
            article_id=line.article_id,
            name=self._text(values.get("name")),
            sku=self._text(values.get("sku")),
            description=self._text(values.get("description")),
            quantity=self._text(values.get("quantity")),
            unit=self._text(values.get("unit")),
            unit_price=self._text(values.get("unit_price")),
            discount_percent=self._text(values.get("discount_percent")),
            tax_rate=self._text(values.get("tax_rate")),
            amount_net=amount_net,
            tax_amount=tax_amount,
            amount_gross=amount_net + tax_amount,
        )

    @staticmethod
    def _totals(lines: tuple[FinanceOfferLineView, ...]) -> FinanceOfferTotals:
        subtotal = Decimal("0")
        net_total = Decimal("0")
        tax_total = Decimal("0")
        for line in lines:
            raw_total = (HubFinanceDocumentService._quantity(line.quantity) * HubFinanceDocumentService._money(line.unit_price)).quantize(_CENT, rounding=ROUND_HALF_UP)
            subtotal += raw_total
            net_total += line.amount_net
            tax_total += line.tax_amount
        return FinanceOfferTotals(
            subtotal_net=subtotal.quantize(_CENT, rounding=ROUND_HALF_UP),
            discount_total=(subtotal - net_total).quantize(_CENT, rounding=ROUND_HALF_UP),
            tax_total=tax_total.quantize(_CENT, rounding=ROUND_HALF_UP),
            total_gross=(net_total + tax_total).quantize(_CENT, rounding=ROUND_HALF_UP),
        )

    def _remaining_amount(self, *, values: dict[str, str], totals: FinanceOfferTotals) -> Decimal:
        if values.get("status") in {"paid", "cancelled"}:
            return Decimal("0")
        return totals.total_gross

    def _billing_address(self, customer: Customer) -> str:
        detail = CustomerDirectoryService(db=self.db, cipher=self.cipher).get_detail(customer_id=customer.id)
        if detail is None:
            return customer.name
        values = [
            field.value.strip()
            for field in detail.profile_fields
            if field.value and field.label in {"Rechnungsadresse - Straße Einzelzeile", "PLZ Ort Hub-Feld"}
        ]
        return "\n".join((customer.name, *values)) if values else customer.name

    def _contact_name(self, contact_id: int | None) -> str:
        if contact_id is None:
            return ""
        return next((entry.name for entry in self.list_linkable_contacts() if entry.id == contact_id), "")

    def _link_label(self, *, module: FinanceDocumentModule, document: Any) -> str:
        if not module.link_attribute:
            return ""
        linked = getattr(document, module.link_attribute)
        if linked is None:
            return ""
        if module is ORDER_MODULE:
            return HubFinanceService.offer_number(linked)
        return self.identifier(module=ORDER_MODULE, document=linked)

    def _values(self, encrypted_values: str) -> dict[str, str]:
        try:
            values = json.loads(self.cipher.decrypt(encrypted_values))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HubFinanceDocumentError("Die gespeicherten Finanzdaten sind ungültig.") from exc
        return {key: self._text(value) for key, value in values.items()} if isinstance(values, dict) else {}

    def _encrypt(self, values: dict[str, str]) -> str:
        return self.cipher.encrypt(json.dumps(values, ensure_ascii=False, separators=(",", ":")))

    @classmethod
    def _limited_text(cls, raw_value: object, label: str) -> str:
        value = cls._text(raw_value).strip()
        if len(value) > cls._MAX_TEXT_LENGTH:
            raise HubFinanceDocumentError(f"{label} ist zu lang.")
        return value

    @staticmethod
    def _text(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _field(fields: tuple[HubFinanceField, ...], key: str) -> HubFinanceField:
        return next(field for field in fields if field.key == key)

    @staticmethod
    def _display_option(field: HubFinanceField, value: object) -> str:
        text = HubFinanceDocumentService._text(value)
        return next((label for option, label in field.options if option == text), text)

    @staticmethod
    def _validate_option(field: HubFinanceField, value: str) -> None:
        if value and field.options and value not in {option for option, _label in field.options}:
            raise HubFinanceDocumentError(f"Die Auswahl für {field.label} ist ungültig.")

    @staticmethod
    def _validate_date(value: str, label: str) -> None:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise HubFinanceDocumentError(f"{label} ist ungültig.") from exc

    @staticmethod
    def _decimal_text(raw_value: object, label: str, *, minimum: Decimal, places: Decimal, maximum: Decimal | None = None) -> str:
        value = HubFinanceDocumentService._decimal(raw_value, label)
        if value < minimum or (maximum is not None and value > maximum):
            raise HubFinanceDocumentError(f"{label} ist ungültig.")
        return HubFinanceDocumentService._decimal_string(value.quantize(places, rounding=ROUND_HALF_UP))

    @staticmethod
    def _integer_text(raw_value: object, label: str, *, minimum: int) -> str:
        try:
            value = int(HubFinanceDocumentService._text(raw_value))
        except ValueError as exc:
            raise HubFinanceDocumentError(f"{label} ist ungültig.") from exc
        if value < minimum:
            raise HubFinanceDocumentError(f"{label} ist ungültig.")
        return str(value)

    @staticmethod
    def _decimal(raw_value: object, label: str) -> Decimal:
        text = HubFinanceDocumentService._text(raw_value).strip().replace(" ", "").replace(",", ".")
        if not text:
            raise HubFinanceDocumentError(f"{label} ist erforderlich.")
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise HubFinanceDocumentError(f"{label} ist ungültig.") from exc

    @staticmethod
    def _decimal_string(value: Decimal) -> str:
        return format(value, "f")

    @staticmethod
    def _money(raw_value: object) -> Decimal:
        try:
            return Decimal(HubFinanceDocumentService._text(raw_value).replace(",", ".") or "0")
        except InvalidOperation:
            return Decimal("0")

    @staticmethod
    def _quantity(raw_value: object) -> Decimal:
        return HubFinanceDocumentService._money(raw_value)

    @staticmethod
    def _percentage(raw_value: object) -> Decimal:
        value = HubFinanceDocumentService._money(raw_value)
        return value if Decimal("0") <= value <= Decimal("100") else Decimal("0")

    @staticmethod
    def _optional_id(value: str, label: str) -> int | None:
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise HubFinanceDocumentError(f"{label} ist ungültig.") from exc

    @staticmethod
    def _truthy(value: object) -> bool:
        return HubFinanceDocumentService._text(value).casefold() in {"true", "1", "on", "yes"}

    @staticmethod
    def _display_date(value: str) -> str:
        try:
            return date.fromisoformat(value).strftime("%d.%m.%Y")
        except ValueError:
            return value
