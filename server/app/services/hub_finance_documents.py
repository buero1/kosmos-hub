"""Shared local persistence for Finance orders, invoices and recurring invoices."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import SecretCipher
from app.core.timezones import format_berlin_time
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_documents import (
    HubFinanceDunning,
    HubFinanceDunningLine,
    HubFinanceInvoice,
    HubFinanceInvoiceLine,
    HubFinanceOrder,
    HubFinanceOrderLine,
    HubFinanceRecurringInvoice,
    HubFinanceRecurringInvoiceLine,
)
from app.models.hub_finance_offer import HubFinanceOffer
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.models.hub_finance_order_pdf import HubFinanceOrderPdf
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.services.finance_invoice_pdf_storage import FinanceInvoicePdfStorage, FinanceInvoicePdfStorageError
from app.services.finance_generated_pdf_storage import FinanceGeneratedPdfStorage
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_finance import (
    FinanceArticleEntry,
    FinanceContactOption,
    FinanceOfferLineView,
    FinanceOfferTotals,
    HubFinanceService,
)
from app.services.hub_finance_document_field_catalog import DUNNING_FIELDS, INVOICE_FIELDS, ORDER_FIELDS, RECURRING_INVOICE_FIELDS
from app.services.hub_finance_field_catalog import FINANCE_POSITION_UNITS, HubFinanceField
from app.services.module_layouts import ModuleLayoutService
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.hub_invoice_email_delivery import InvoiceEmailDelivery, invoice_email_deliveries


ORDER_FIELDS_LAYOUT_KEY = "finance-order-fields"
INVOICE_FIELDS_LAYOUT_KEY = "finance-invoice-fields"
DUNNING_FIELDS_LAYOUT_KEY = "finance-dunning-fields"
RECURRING_INVOICE_FIELDS_LAYOUT_KEY = "finance-recurring-invoice-fields"
_CENT = Decimal("0.01")
_QUANTITY_STEP = Decimal("0.01")


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
    is_dunning: bool = False
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

DUNNING_MODULE = FinanceDocumentModule(
    key="dunnings",
    label="Mahnungen",
    singular="Mahnung",
    eyebrow="Finance · Mahnung",
    fields=DUNNING_FIELDS,
    layout_key=DUNNING_FIELDS_LAYOUT_KEY,
    model=HubFinanceDunning,
    line_model=HubFinanceDunningLine,
    number_attribute="dunning_number",
    number_prefix="MAH",
    date_key="dunning_date",
    link_key="linked_invoice",
    link_attribute="invoice",
    link_model=HubFinanceInvoice,
    is_dunning=True,
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
    for module in (ORDER_MODULE, INVOICE_MODULE, DUNNING_MODULE, RECURRING_INVOICE_MODULE)
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
class FinanceDunningDraft:
    invoice_id: int
    invoice_identifier: str
    customer_id: int | None
    contact_id: int | None
    submitted_values: dict[str, str]


@dataclass(frozen=True)
class FinanceDocumentEntry:
    document: Any
    identifier: str
    status: str
    customer_name: str
    document_date: str
    total_gross: str
    email_delivery: InvoiceEmailDelivery | None = None


@dataclass(frozen=True)
class FinanceDocumentPage:
    entries: tuple[FinanceDocumentEntry, ...]
    page: int
    page_count: int
    total_count: int


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
    show_more_index: int
    billing_address: str = ""
    invoice_pdf: "FinanceInvoicePdfView | None" = None
    order_pdf: "FinanceOrderPdfView | None" = None
    email_delivery: InvoiceEmailDelivery | None = None


@dataclass(frozen=True)
class FinanceInvoicePdfView:
    filename: str
    byte_size: int
    is_zugferd: bool


@dataclass(frozen=True)
class FinanceOrderPdfView:
    filename: str
    byte_size: int


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
        return self._sorted_document_entries(module=module, documents=documents)

    def list_customer_documents(
        self,
        *,
        module: FinanceDocumentModule,
        customer_id: int,
    ) -> tuple[FinanceDocumentEntry, ...]:
        documents = self.db.scalars(
            select(module.model)
            .options(selectinload(module.model.customer), selectinload(module.model.lines))
            .where(module.model.customer_id == customer_id)
        ).all()
        return self._sorted_document_entries(module=module, documents=documents)

    def _sorted_document_entries(
        self,
        *,
        module: FinanceDocumentModule,
        documents: list[Any],
    ) -> tuple[FinanceDocumentEntry, ...]:
        deliveries = invoice_email_deliveries(self.db, [doc.id for doc in documents]) if module.is_invoice else {}
        entries = [self._entry(module=module, document=document, email_delivery=deliveries.get(document.id)) for document in documents]
        return tuple(sorted(
            entries,
            key=lambda entry: (
                self._text(self._document_values(module=module, document=entry.document).get(module.date_key)),
                entry.document.created_at.isoformat() if entry.document.created_at else "",
                entry.document.id,
            ),
            reverse=True,
        ))

    def list_invoice_page(self, *, page: int, page_size: int = 100, allowed_customer_ids: set[int] | None = None, include_orphans: bool = True) -> FinanceDocumentPage:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        module = INVOICE_MODULE
        query = select(module.model.id, module.model.encrypted_fields_json, module.model.created_at)
        if allowed_customer_ids is not None:
            query = query.where(module.model.customer_id.in_(allowed_customer_ids))
        if not include_orphans:
            query = query.where(module.model.customer_id.is_not(None))
        rows = self.db.execute(query).all()
        ordered_ids = [row.id for row in sorted(
            rows,
            key=lambda row: (
                self._text(self._values(row.encrypted_fields_json).get(module.date_key)),
                row.created_at.isoformat() if row.created_at else "",
                row.id,
            ),
            reverse=True,
        )]
        total_count = len(ordered_ids)
        page_count = max(1, (total_count + page_size - 1) // page_size)
        page = min(max(1, page), page_count)
        page_ids = ordered_ids[(page - 1) * page_size:page * page_size]
        if not page_ids:
            return FinanceDocumentPage(entries=(), page=page, page_count=page_count, total_count=total_count)
        documents = self.db.scalars(
            select(module.model)
            .options(selectinload(module.model.customer), selectinload(module.model.lines))
            .where(module.model.id.in_(page_ids))
        ).all()
        by_id = {document.id: document for document in documents}
        deliveries = invoice_email_deliveries(self.db, page_ids)
        entries = tuple(self._entry(module=module, document=by_id[document_id], email_delivery=deliveries[document_id]) for document_id in page_ids)
        return FinanceDocumentPage(entries=entries, page=page, page_count=page_count, total_count=total_count)

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
        values = self._document_values(module=module, document=document)
        currency = self._text(values.get("currency")) or "EUR"
        lines = tuple(replace(self._line_view(line), currency=currency) for line in document.lines)
        totals = replace(self._totals(lines), currency=currency)
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
        if module is ORDER_MODULE:
            enriched_values["created_time"] = values.get("created_time") or (document.created_at.isoformat() if document.created_at else "")
            enriched_values["modified_time"] = values.get("modified_time") or (document.updated_at.isoformat() if document.updated_at else "")
        if module.is_invoice:
            enriched_values["remaining_amount"] = self._decimal_string(self._remaining_amount(values=values, totals=totals))
        fields, show_more_index = self._field_values_with_show_more(module=module, values=enriched_values)
        invoice_pdf = self._invoice_pdf_view(document.id) if module.is_invoice else None
        order_pdf = self._order_pdf_view(document.id) if module is ORDER_MODULE else None
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
            show_more_index=show_more_index,
            billing_address=self._text(values.get("billing_address")) if module.is_invoice or module.is_dunning else "",
            invoice_pdf=invoice_pdf,
            order_pdf=order_pdf,
            email_delivery=invoice_email_deliveries(self.db, [document.id])[document.id] if module.is_invoice else None,
        )

    @staticmethod
    def new_form_values(*, module: FinanceDocumentModule) -> dict[str, str]:
        today = date.today().isoformat()
        values = {
            "document_field__status": "active" if module.is_recurring else "draft",
            "document_line__0__quantity": "1",
            "document_line__0__tax_rate": "19",
            "document_line__0__discount_percent": "0",
        }
        if module is not ORDER_MODULE:
            values["document_field__currency"] = "EUR"
        if module is ORDER_MODULE:
            values["document_field__order_date"] = today
        elif module is INVOICE_MODULE:
            values["document_field__invoice_date"] = today
            values["document_field__due_date"] = today
        elif module is DUNNING_MODULE:
            values["document_field__status"] = "payment_reminder"
            values["document_field__dunning_date"] = today
            values["document_field__due_date"] = (date.today() + timedelta(days=7)).isoformat()
        else:
            values.update({
                "document_field__start_date": today,
                "document_field__next_invoice_date": today,
                "document_field__interval_unit": "month",
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
        pdf_template_id: int | None = None,
    ) -> Any:
        customer, contact = self._customer_and_contact(customer_id=customer_id, contact_id=contact_id)
        linked_record = self._linked_record(module=module, link_id=link_id, customer_id=customer.id)
        values = self._submitted_fields(module=module, submitted_values=submitted_values)
        if module is ORDER_MODULE:
            created_time = datetime.now(UTC).isoformat()
            values["created_time"] = created_time
            values["modified_time"] = created_time
        if module.is_invoice or module.is_dunning:
            values["billing_address"] = self._billing_address(customer)
        if module.is_recurring:
            values.update({
                "interval_count": "1",
                "automatic_creation": "true",
                "late_fee": "0.00",
                "system_payment_complete": "false",
            })
        constructor: dict[str, Any] = {"customer": customer, "contact": contact, "encrypted_fields_json": self._encrypt(values)}
        if module in (ORDER_MODULE, INVOICE_MODULE, DUNNING_MODULE, RECURRING_INVOICE_MODULE):
            constructor["pdf_template"] = self._pdf_template(module=module, template_id=pdf_template_id)
        if module.link_attribute:
            constructor[module.link_attribute] = linked_record
        document = module.model(**constructor)
        if module.is_recurring:
            document.hub_next_run_on = date.fromisoformat(values["next_invoice_date"]) if values["next_invoice_date"] else None
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
        pdf_template_id: int | None = None,
    ) -> Any:
        query = select(module.model).options(selectinload(module.model.lines)).where(module.model.id == document_id)
        if module.is_recurring:
            query = query.with_for_update().execution_options(populate_existing=True)
        document = self.db.scalar(query)
        if document is None:
            raise HubFinanceDocumentError(f"{module.singular} wurde nicht gefunden.")
        customer, contact = self._customer_and_contact(customer_id=customer_id, contact_id=contact_id)
        linked_record = self._linked_record(module=module, link_id=link_id, customer_id=customer.id)
        existing_values = self._document_values(module=module, document=document)
        values = self._submitted_fields(module=module, submitted_values=submitted_values, existing_values=existing_values)
        if module.is_recurring:
            interval_changed = values["interval_unit"] != existing_values.get("interval_unit")
            values = {**existing_values, **values}
            if interval_changed:
                values["interval_count"] = "1"
        if module is ORDER_MODULE:
            values["created_time"] = existing_values.get("created_time") or (document.created_at.isoformat() if document.created_at else "")
            values["modified_time"] = datetime.now(UTC).isoformat()
            values["currency"] = existing_values.get("currency") or "EUR"
        if module.is_invoice or module.is_dunning:
            values["billing_address"] = existing_values.get("billing_address") or self._billing_address(customer)
        if module.is_recurring:
            document.hub_next_run_on = date.fromisoformat(values["next_invoice_date"]) if values["next_invoice_date"] else None
        document.customer = customer
        document.contact = contact
        if module in (ORDER_MODULE, INVOICE_MODULE, DUNNING_MODULE, RECURRING_INVOICE_MODULE):
            document.pdf_template = self._pdf_template(module=module, template_id=pdf_template_id)
        if module.link_attribute:
            setattr(document, module.link_attribute, linked_record)
        document.encrypted_fields_json = self._encrypt(values)
        self._replace_lines(module=module, document=document, submitted_values=submitted_values)
        if module is ORDER_MODULE:
            document.unassigned_owner_user_id = None
        self.db.flush()
        return document

    def delete_document(self, *, module: FinanceDocumentModule, document_id: int, after_commit=None) -> Any:
        document = self.db.get(module.model, document_id)
        if document is None:
            raise HubFinanceDocumentError(f"{module.singular} wurde nicht gefunden.")
        if module.is_invoice:
            pdf = self.db.scalar(select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == document.id))
            if pdf is not None:
                self._remove_pdf_after_commit(FinanceInvoicePdfStorage(cipher=self.cipher), pdf.storage_key, after_commit)
                self.db.delete(pdf)
        elif module is ORDER_MODULE:
            pdf = self.db.scalar(select(HubFinanceOrderPdf).where(HubFinanceOrderPdf.order_id == document.id))
            if pdf is not None:
                self._remove_pdf_after_commit(FinanceInvoicePdfStorage(cipher=self.cipher), pdf.storage_key, after_commit)
                self.db.delete(pdf)
        if module in (ORDER_MODULE, INVOICE_MODULE, DUNNING_MODULE):
            generated_pdf = self.db.scalar(
                select(HubFinanceGeneratedPdf).where(
                    HubFinanceGeneratedPdf.document_type == module.key,
                    HubFinanceGeneratedPdf.document_id == document.id,
                )
            )
            if generated_pdf is not None:
                if generated_pdf.storage_key:
                    self._remove_pdf_after_commit(FinanceGeneratedPdfStorage(cipher=self.cipher), generated_pdf.storage_key, after_commit)
                self.db.delete(generated_pdf)
        self.db.delete(document)
        self.db.flush()
        return document

    @staticmethod
    def _remove_pdf_after_commit(storage, storage_key, after_commit):
        if after_commit is None:
            storage.remove(storage_key)
        else:
            after_commit(f"delete-finance-pdf:{storage_key}", lambda: storage.remove(storage_key))

    def _pdf_template(self, *, module: FinanceDocumentModule, template_id: int | None):
        document_type = "invoices" if module.is_recurring else module.key
        service = HubPdfTemplateService(db=self.db)
        template = service.get(template_id) if template_id else service.default_for(document_type)
        if template is None or template.document_type != document_type:
            raise HubFinanceDocumentError("Die gewählte PDF-Vorlage passt nicht zur Belegart.")
        return template

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
        if module is DUNNING_MODULE:
            return tuple(
                FinanceDocumentLinkOption(id=entry.document.id, label=entry.identifier, customer_id=entry.document.customer_id)
                for entry in self.list_documents(module=INVOICE_MODULE)
            )
        return ()

    def dunning_draft_from_invoice(self, *, invoice_id: int) -> FinanceDunningDraft:
        detail = self.get_detail(module=INVOICE_MODULE, document_id=invoice_id)
        if detail is None:
            raise HubFinanceDocumentError("Die verknüpfte Rechnung wurde nicht gefunden.")
        invoice_values = self._document_values(module=INVOICE_MODULE, document=detail.document)
        submitted = self.new_form_values(module=DUNNING_MODULE)
        submitted["document_field__currency"] = self._text(invoice_values.get("currency")) or "EUR"
        for index, line in enumerate(detail.lines):
            submitted.update({
                f"document_line__{index}__article_id": str(line.article_id or ""),
                f"document_line__{index}__name": line.name,
                f"document_line__{index}__sku": line.sku,
                f"document_line__{index}__description": line.description,
                f"document_line__{index}__quantity": line.quantity,
                f"document_line__{index}__unit": line.unit,
                f"document_line__{index}__unit_price": line.unit_price,
                f"document_line__{index}__discount_percent": line.discount_percent,
                f"document_line__{index}__tax_rate": line.tax_rate,
            })
        return FinanceDunningDraft(
            invoice_id=detail.document.id,
            invoice_identifier=detail.identifier,
            customer_id=detail.document.customer_id,
            contact_id=detail.document.contact_id,
            submitted_values=submitted,
        )

    def identifier(self, *, module: FinanceDocumentModule, document: Any, values: dict[str, str] | None = None) -> str:
        values = values if values is not None else self._document_values(module=module, document=document)
        if module.number_attribute:
            return self._text(getattr(document, module.number_attribute)) or f"{module.number_prefix}-{document.id:06d}"
        return self._text(values.get("name")) or f"Periodische Rechnung {document.id}"

    def _entry(self, *, module: FinanceDocumentModule, document: Any, email_delivery: InvoiceEmailDelivery | None = None) -> FinanceDocumentEntry:
        values = self._document_values(module=module, document=document)
        lines = tuple(self._line_view(line) for line in document.lines)
        currency = self._text(values.get("currency")) or "EUR"
        return FinanceDocumentEntry(
            document=document,
            identifier=self.identifier(module=module, document=document, values=values),
            status=self._display_option(self._field(module.fields, "status"), values.get("status")) or "-",
            customer_name=document.customer.name if document.customer else "-",
            document_date=self._display_date(self._text(values.get(module.date_key))) or "-",
            total_gross=HubFinanceService.format_money(self._totals(lines).total_gross, currency),
            email_delivery=email_delivery,
        )

    def _document_values(self, *, module: FinanceDocumentModule, document: Any) -> dict[str, str]:
        values = self._values(document.encrypted_fields_json)
        if module.is_recurring:
            if values.get("status") in {"paused", "ended"}:
                values["next_invoice_date"] = ""
            elif document.hub_next_run_on is not None:
                values["next_invoice_date"] = document.hub_next_run_on.isoformat()
        return values

    def _field_values(
        self,
        *,
        module: FinanceDocumentModule,
        values: dict[str, str],
    ) -> tuple[FinanceDocumentFieldValue, ...]:
        fields, _ = self._field_values_with_show_more(module=module, values=values)
        return fields

    def _field_values_with_show_more(
        self,
        *,
        module: FinanceDocumentModule,
        values: dict[str, str],
    ) -> tuple[tuple[FinanceDocumentFieldValue, ...], int]:
        ordered_keys, show_more_index = ModuleLayoutService(db=self.db).ordered_keys_with_show_more(
            layout_key=module.layout_key,
            default_keys=tuple(field.key for field in module.fields),
        )
        by_key = {field.key: field for field in module.fields}
        result = []
        for key in ordered_keys:
            definition = by_key[key]
            raw_value = self._text(values.get(key))
            value = raw_value
            form_value = raw_value
            options = definition.options
            if module.is_recurring and key == "custom_interval":
                if values.get("interval_unit") == "custom":
                    count = self._text(values.get("custom_interval_count"))
                    unit = self._text(values.get("custom_interval_unit"))
                    unit_labels = {"day": ("Tag", "Tage"), "week": ("Woche", "Wochen"), "month": ("Monat", "Monate"), "year": ("Jahr", "Jahre")}
                    singular, plural = unit_labels.get(unit, ("", ""))
                    value = f"{count} {singular if count == '1' else plural}".strip()
                    form_value = f"{count}:{unit}"
                else:
                    value = ""
                    form_value = ""
            if module.is_recurring and key == "payment_due":
                count, unit = self._payment_due_parts(values)
                unit_labels = {"day": ("Tag", "Tage"), "week": ("Woche", "Wochen"), "year": ("Jahr", "Jahre")}
                singular, plural = unit_labels.get(unit, ("", ""))
                value = f"{count} {singular if count == '1' else plural}".strip() if count else ""
                form_value = f"{count}:{unit}" if count or unit else ""
            if definition.display_type == "Auswahlliste":
                value = self._display_option(definition, raw_value)
                if module.is_recurring and key == "interval_unit":
                    try:
                        interval_count = int(values.get("interval_count", "1"))
                    except (TypeError, ValueError):
                        interval_count = 1
                    if interval_count > 1 and raw_value in {"month", "quarter", "year"}:
                        unit = {"month": "Monate", "quarter": "Quartale", "year": "Jahre"}[raw_value]
                        value = f"Alle {interval_count} {unit}"
                        options = tuple(
                            (option_key, value if option_key == raw_value else option_label)
                            for option_key, option_label in options
                        )
            elif definition.display_type == "Waehrung":
                value = HubFinanceService.format_money(self._money(raw_value))
            elif definition.display_type == "Datum":
                value = self._display_date(raw_value)
            elif definition.display_type == "DatumZeit":
                value = format_berlin_time(raw_value)
            elif definition.display_type == "Dezimalzahl":
                value = HubFinanceService.format_quantity(self._money(raw_value)) if raw_value else ""
            elif definition.display_type == "Boolesch":
                value = "Ja" if self._truthy(raw_value) else "Nein"
            result.append(FinanceDocumentFieldValue(
                key=definition.key,
                label=definition.label,
                display_type=definition.display_type,
                value=value,
                form_value=value if definition.read_only else form_value,
                required=(values.get("status") == "active") if module.is_recurring and key == "next_invoice_date" else definition.required,
                read_only=definition.read_only,
                options=options,
            ))
        return tuple(result), show_more_index

    def _submitted_fields(
        self,
        *,
        module: FinanceDocumentModule,
        submitted_values: dict[str, str],
        existing_values: dict[str, str] | None = None,
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        for definition in module.fields:
            if definition.read_only or definition.key in {"customer", "contact", module.link_key} or (module.is_recurring and definition.key in {"custom_interval", "payment_due"}):
                continue
            if module.is_recurring and definition.key == "next_invoice_date" and self._text(submitted_values.get("document_field__status")).strip() in {"paused", "ended"}:
                # Ignore even stale submitted dates when the schedule is stopped.
                values[definition.key] = ""
                continue
            raw = self._limited_text(submitted_values.get(f"document_field__{definition.key}"), definition.label)
            if definition.display_type == "Boolesch":
                raw = "true" if self._truthy(raw) else "false"
            if definition.required and not raw and not (
                module is INVOICE_MODULE
                and definition.key == "due_date"
                and submitted_values.get("document_field__payment_terms")
            ):
                raise HubFinanceDocumentError(f"{definition.label} ist erforderlich.")
            if definition.display_type == "Datum" and raw:
                self._validate_date(raw, definition.label)
            elif definition.display_type == "Ganzzahl" and raw:
                raw = self._integer_text(raw, definition.label, minimum=1)
            elif definition.display_type == "Waehrung" and raw:
                raw = self._decimal_text(raw, definition.label, minimum=Decimal("0"), places=_CENT)
            elif definition.display_type == "Dezimalzahl" and raw:
                raw = self._decimal_text(raw, definition.label, minimum=Decimal("0"), places=_CENT)
            self._validate_option(definition, raw)
            values[definition.key] = raw
        if module is INVOICE_MODULE:
            terms = values.get("payment_terms", "")
            if terms:
                expected_due_date = self._invoice_due_date(values["invoice_date"], terms)
                if existing_values is None:
                    if values["due_date"] in {"", values["invoice_date"], expected_due_date}:
                        values["due_date"] = expected_due_date
                    else:
                        values["payment_terms"] = ""
                elif values["due_date"] != existing_values.get("due_date"):
                    if values["due_date"] != expected_due_date:
                        values["payment_terms"] = ""
                elif (
                    values["invoice_date"] != existing_values.get("invoice_date")
                    or terms != existing_values.get("payment_terms")
                ):
                    values["due_date"] = expected_due_date
            if values.get("due_date", "") < values.get("invoice_date", ""):
                raise HubFinanceDocumentError("Fällig am darf nicht vor dem Rechnungsdatum liegen.")
        if module is RECURRING_INVOICE_MODULE and values.get("end_date") and values["end_date"] < values.get("start_date", ""):
            raise HubFinanceDocumentError("Enddatum darf nicht vor dem Startdatum liegen.")
        if module.is_recurring:
            if values["interval_unit"] == "custom":
                raw_count = self._limited_text(submitted_values.get("document_field__custom_interval_count"), "Rhythmus Benutzerdefiniert")
                raw_unit = self._limited_text(submitted_values.get("document_field__custom_interval_unit"), "Rhythmus Benutzerdefiniert")
                if not raw_count or not raw_unit:
                    raise HubFinanceDocumentError("Rhythmus Benutzerdefiniert ist erforderlich.")
                count = self._integer_text(raw_count, "Rhythmus Benutzerdefiniert", minimum=1)
                if int(count) > 9999 or raw_unit not in {"day", "week", "month", "year"}:
                    raise HubFinanceDocumentError("Rhythmus Benutzerdefiniert ist ungültig.")
                values["custom_interval_count"] = count
                values["custom_interval_unit"] = raw_unit
            else:
                values["custom_interval_count"] = ""
                values["custom_interval_unit"] = ""
            due_count = self._limited_text(submitted_values.get("document_field__payment_due_count"), "Zahlungsziel")
            due_unit = self._limited_text(submitted_values.get("document_field__payment_due_unit"), "Zahlungsziel")
            if due_count or due_unit:
                if not due_count or not due_unit:
                    raise HubFinanceDocumentError("Zahlungsziel benötigt Anzahl und Zeiteinheit.")
                normalized_count = self._integer_text(due_count, "Zahlungsziel", minimum=0)
                if int(normalized_count) > 9999 or due_unit not in {"day", "week", "year"}:
                    raise HubFinanceDocumentError("Zahlungsziel ist ungültig.")
                values["payment_due_count"] = normalized_count
                values["payment_due_unit"] = due_unit
                values["payment_terms"] = (
                    "due_on_receipt" if due_unit == "day" and normalized_count == "0"
                    else f"{normalized_count}_days" if due_unit == "day" else ""
                )
            else:
                values["payment_due_count"] = ""
                values["payment_due_unit"] = ""
                values["payment_terms"] = ""
        return values

    @staticmethod
    def _invoice_due_date(invoice_date: str, payment_terms: str) -> str:
        days = 0 if payment_terms == "due_on_receipt" else int(payment_terms.removesuffix("_days"))
        return (date.fromisoformat(invoice_date) + timedelta(days=days)).isoformat()

    @staticmethod
    def _payment_due_parts(values: dict[str, str]) -> tuple[str, str]:
        count = values.get("payment_due_count", "")
        unit = values.get("payment_due_unit", "")
        if count or unit:
            return count, unit
        legacy = values.get("payment_terms", "")
        if legacy == "due_on_receipt":
            return "0", "day"
        match = re.fullmatch(r"(\d+)_days", legacy)
        return (match.group(1), "day") if match else ("", "")

    def _replace_lines(self, *, module: FinanceDocumentModule, document: Any, submitted_values: dict[str, str]) -> None:
        document.lines.clear()
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
            unit = self._limited_text(row.get("unit"), "Position: Einheit")
            if unit and unit not in FINANCE_POSITION_UNITS:
                raise HubFinanceDocumentError("Die Auswahl für Position: Einheit ist ungültig.")
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
                "unit": unit,
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
            raise HubFinanceDocumentError("Ansprechpartner ist erforderlich.")
        contact = self.db.get(CustomerContact, contact_id)
        if contact is None or contact.customer_id != customer.id:
            raise HubFinanceDocumentError("Der Ansprechpartner gehört nicht zum ausgewählten Kunden.")
        return customer, contact

    def _linked_record(self, *, module: FinanceDocumentModule, link_id: int | None, customer_id: int) -> Any | None:
        if module.link_model is None:
            return None
        if link_id is None:
            if module.link_key and self._field(module.fields, module.link_key).required:
                raise HubFinanceDocumentError(f"{self._field(module.fields, module.link_key).label} ist erforderlich.")
            return None
        record = self.db.get(module.link_model, link_id)
        lead_offer = module is ORDER_MODULE and record is not None and record.customer_id is None and record.lead_id is not None
        if record is None or (record.customer_id != customer_id and not lead_offer):
            raise HubFinanceDocumentError("Der verknüpfte Beleg gehört nicht zum ausgewählten Kunden.")
        return record

    def _line_view(self, line: Any) -> FinanceOfferLineView:
        values = self._values(line.encrypted_fields_json)
        name, description = self._free_text_values(
            article_id=line.article_id,
            name=self._text(values.get("name")),
            description=self._text(values.get("description")),
        )
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
            name=name,
            sku=self._text(values.get("sku")),
            description=description,
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
    def _free_text_values(*, article_id: int | None, name: str, description: str) -> tuple[str, str]:
        if (
            article_id is None
            and description.strip()
            and (not name.strip() or name.strip().casefold() in {"freitextposition", "freitext position"})
        ):
            return description, ""
        return name, description

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
        imported_balance = values.get("remaining_amount")
        if imported_balance:
            return self._money(imported_balance)
        if values.get("status") in {"paid", "cancelled"}:
            return Decimal("0")
        return totals.total_gross

    def _invoice_pdf_view(self, invoice_id: int) -> FinanceInvoicePdfView | None:
        pdf = self.db.scalar(select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == invoice_id))
        if pdf is None:
            return None
        return FinanceInvoicePdfView(filename=pdf.filename, byte_size=pdf.byte_size, is_zugferd=pdf.is_zugferd)

    def _order_pdf_view(self, order_id: int) -> FinanceOrderPdfView | None:
        pdf = self.db.scalar(select(HubFinanceOrderPdf).where(HubFinanceOrderPdf.order_id == order_id))
        if pdf is None:
            return None
        return FinanceOrderPdfView(filename=pdf.filename, byte_size=pdf.byte_size)

    def load_invoice_pdf(self, *, invoice_id: int):
        pdf = self.db.scalar(select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == invoice_id))
        if pdf is None:
            raise HubFinanceDocumentError("Für diese Rechnung ist noch keine PDF-Vorschau vorhanden.")
        try:
            content = FinanceInvoicePdfStorage(cipher=self.cipher).load(pdf.storage_key)
        except FinanceInvoicePdfStorageError as exc:
            raise HubFinanceDocumentError(str(exc)) from exc
        return pdf, content

    def load_order_pdf(self, *, order_id: int):
        pdf = self.db.scalar(select(HubFinanceOrderPdf).where(HubFinanceOrderPdf.order_id == order_id))
        if pdf is None:
            raise HubFinanceDocumentError("Für diesen Auftrag ist noch keine PDF-Vorschau vorhanden.")
        try:
            content = FinanceInvoicePdfStorage(cipher=self.cipher).load(pdf.storage_key)
        except FinanceInvoicePdfStorageError as exc:
            message = str(exc).replace("Rechnungs-PDF", "Auftrags-PDF")
            raise HubFinanceDocumentError(message) from exc
        return pdf, content

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
        if module is INVOICE_MODULE:
            return self.identifier(module=ORDER_MODULE, document=linked)
        return self.identifier(module=INVOICE_MODULE, document=linked)

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
