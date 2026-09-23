"""Local Finance modules with encrypted fields and calculated offer totals."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import unescape
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_offer import HubFinanceOffer, HubFinanceOfferLine
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_lead import HubLead
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_finance_field_catalog import ARTICLE_FIELDS, FINANCE_POSITION_UNITS, OFFER_FIELDS, HubFinanceField
from app.services.hub_leads import HubLeadService
from app.services.module_layouts import ModuleLayoutService
from app.services.finance_generated_pdf_storage import FinanceGeneratedPdfStorage
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.hub_offer_notes import sanitize_offer_notes


ARTICLE_FIELDS_LAYOUT_KEY = "finance-article-fields"
OFFER_FIELDS_LAYOUT_KEY = "finance-offer-fields"
_CENT = Decimal("0.01")
_QUANTITY_STEP = Decimal("0.01")


class HubFinanceError(ValueError):
    """A safe validation message for Finance pages."""


@dataclass(frozen=True)
class HubFinanceFieldValue:
    key: str
    label: str
    display_type: str
    value: str
    form_value: str
    required: bool = False
    read_only: bool = False
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FinanceArticleEntry:
    article: HubFinanceArticle
    name: str
    sku: str
    status: str
    kind: str
    net_price: str
    tax_rate: str
    unit: str
    net_price_form: str
    tax_rate_value: str


@dataclass(frozen=True)
class FinanceArticleDetail:
    article: HubFinanceArticle
    name: str
    fields: tuple[HubFinanceFieldValue, ...]
    show_more_index: int


@dataclass(frozen=True)
class FinanceContactOption:
    id: int
    name: str
    customer_id: int
    customer_name: str


@dataclass(frozen=True)
class FinanceLeadOption:
    id: int
    name: str
    company: str
    email: str

    @property
    def label(self) -> str:
        details = [self.name]
        if self.company and self.company != "-" and self.company.casefold() != self.name.casefold():
            details.append(self.company)
        if self.email and self.email != "-":
            details.append(self.email)
        return " · ".join(details)


@dataclass(frozen=True)
class FinanceOfferLineView:
    id: int | None
    article_id: int | None
    name: str
    sku: str
    description: str
    quantity: str
    unit: str
    unit_price: str
    discount_percent: str
    tax_rate: str
    amount_net: Decimal
    tax_amount: Decimal
    amount_gross: Decimal
    currency: str = "EUR"

    @property
    def unit_price_display(self) -> str:
        return HubFinanceService.format_money(HubFinanceService._money(self.unit_price), self.currency)

    @property
    def amount_net_display(self) -> str:
        return HubFinanceService.format_money(self.amount_net, self.currency)


@dataclass(frozen=True)
class FinanceOfferTotals:
    subtotal_net: Decimal
    discount_total: Decimal
    tax_total: Decimal
    total_gross: Decimal
    currency: str = "EUR"

    @property
    def subtotal_net_display(self) -> str:
        return HubFinanceService.format_money(self.subtotal_net, self.currency)

    @property
    def discount_total_display(self) -> str:
        return HubFinanceService.format_money(self.discount_total, self.currency)

    @property
    def tax_total_display(self) -> str:
        return HubFinanceService.format_money(self.tax_total, self.currency)

    @property
    def total_gross_display(self) -> str:
        return HubFinanceService.format_money(self.total_gross, self.currency)


@dataclass(frozen=True)
class FinanceOfferEntry:
    offer: HubFinanceOffer
    offer_number: str
    status: str
    linked_name: str
    linked_kind: str
    linked_href: str
    offer_date: str
    total_gross: str
    currency: str


@dataclass(frozen=True)
class FinanceOfferDetail:
    offer: HubFinanceOffer
    offer_number: str
    status: str
    linked_name: str
    linked_kind: str
    linked_href: str
    contact_name: str
    fields: tuple[HubFinanceFieldValue, ...]
    lines: tuple[FinanceOfferLineView, ...]
    totals: FinanceOfferTotals
    show_more_index: int
    notes_html: str = ""

    @property
    def header_fields(self) -> tuple[HubFinanceFieldValue, ...]:
        return tuple(field for field in self.fields if field.key != "notes")


class HubFinanceService:
    """Persist the first Finance modules without coupling them to Zoho Books."""

    _MAX_TEXT_LENGTH = 20_000

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_articles(self) -> tuple[FinanceArticleEntry, ...]:
        entries = []
        for article in self.db.scalars(select(HubFinanceArticle)).all():
            values = self._values(article.encrypted_fields_json)
            entries.append(
                FinanceArticleEntry(
                    article=article,
                    name=self._text(values.get("name")) or "Ohne Name",
                    sku=self._text(values.get("sku")) or "-",
                    status=self._display_option(self._field(ARTICLE_FIELDS, "status"), values.get("status")) or "-",
                    kind=self._display_option(self._field(ARTICLE_FIELDS, "kind"), values.get("kind")) or "-",
                    net_price=self.format_money(self._money(values.get("net_price"))),
                    tax_rate=self._display_option(self._field(ARTICLE_FIELDS, "tax_rate"), values.get("tax_rate")) or "-",
                    unit=self._text(values.get("unit")),
                    net_price_form=self._text(values.get("net_price")),
                    tax_rate_value=self._text(values.get("tax_rate")),
                )
            )
        return tuple(sorted(entries, key=lambda entry: (entry.name.casefold(), entry.article.id)))

    def get_article_detail(self, *, article_id: int) -> FinanceArticleDetail | None:
        article = self.db.get(HubFinanceArticle, article_id)
        if article is None:
            return None
        values = self._values(article.encrypted_fields_json)
        fields, show_more_index = self._field_values(
            definitions=ARTICLE_FIELDS,
            values=values,
            layout_key=ARTICLE_FIELDS_LAYOUT_KEY,
        )
        return FinanceArticleDetail(
            article=article,
            name=self._text(values.get("name")) or "Artikel",
            fields=fields,
            show_more_index=show_more_index,
        )

    @staticmethod
    def new_article_values() -> dict[str, str]:
        return {
            "article_field__status": "active",
            "article_field__kind": "service",
            "article_field__tax_rate": "19",
            "article_field__unit": "Stück",
        }

    def create_article(self, *, submitted_values: dict[str, str]) -> HubFinanceArticle:
        article = HubFinanceArticle(encrypted_fields_json=self._encrypt(self._submitted_article_values(submitted_values)))
        self.db.add(article)
        self.db.flush()
        return article

    def update_article(self, *, article_id: int, submitted_values: dict[str, str]) -> HubFinanceArticle:
        article = self.db.get(HubFinanceArticle, article_id)
        if article is None:
            raise HubFinanceError("Der Artikel wurde nicht gefunden.")
        article.encrypted_fields_json = self._encrypt(self._submitted_article_values(submitted_values))
        self.db.flush()
        return article

    def delete_article(self, *, article_id: int) -> HubFinanceArticle:
        article = self.db.get(HubFinanceArticle, article_id)
        if article is None:
            raise HubFinanceError("Der Artikel wurde nicht gefunden.")
        self.db.delete(article)
        self.db.flush()
        return article

    def list_offers(self) -> tuple[FinanceOfferEntry, ...]:
        offers = self.db.scalars(
            select(HubFinanceOffer).options(
                selectinload(HubFinanceOffer.customer),
                selectinload(HubFinanceOffer.lead),
                selectinload(HubFinanceOffer.lines),
            )
        ).all()
        return self._sorted_offer_entries(offers)

    def list_customer_offers(self, *, customer_id: int) -> tuple[FinanceOfferEntry, ...]:
        offers = self.db.scalars(
            select(HubFinanceOffer)
            .options(
                selectinload(HubFinanceOffer.customer),
                selectinload(HubFinanceOffer.lead),
                selectinload(HubFinanceOffer.lines),
            )
            .where(HubFinanceOffer.customer_id == customer_id)
        ).all()
        return self._sorted_offer_entries(offers)

    def list_lead_offers(self, *, lead_id: int) -> tuple[FinanceOfferEntry, ...]:
        offers = self.db.scalars(
            select(HubFinanceOffer)
            .options(
                selectinload(HubFinanceOffer.customer),
                selectinload(HubFinanceOffer.lead),
                selectinload(HubFinanceOffer.lines),
            )
            .where(HubFinanceOffer.lead_id == lead_id)
        ).all()
        return self._sorted_offer_entries(offers)

    def _sorted_offer_entries(self, offers: list[HubFinanceOffer]) -> tuple[FinanceOfferEntry, ...]:
        entries = [self._offer_entry(offer) for offer in offers]
        return tuple(sorted(entries, key=lambda entry: (entry.offer_date, entry.offer.created_at.isoformat() if entry.offer.created_at else "", entry.offer.id), reverse=True))

    def get_offer_detail(self, *, offer_id: int) -> FinanceOfferDetail | None:
        offer = self.db.scalar(
            select(HubFinanceOffer)
            .options(
                selectinload(HubFinanceOffer.customer),
                selectinload(HubFinanceOffer.lead),
                selectinload(HubFinanceOffer.contact),
                selectinload(HubFinanceOffer.lines).selectinload(HubFinanceOfferLine.article),
            )
            .where(HubFinanceOffer.id == offer_id)
        )
        if offer is None:
            return None
        values = self._values(offer.encrypted_fields_json)
        lead_detail = self._lead_detail(offer.lead_id)
        linked_name = offer.customer.name if offer.customer is not None else lead_detail.name if lead_detail is not None else ""
        contact_name = self._contact_name(offer.contact_id) or (lead_detail.name if lead_detail is not None else "")
        enriched_values = {
            **values,
            "offer_number": self.offer_number(offer),
            "customer": linked_name,
            "contact": contact_name,
        }
        fields, show_more_index = self._field_values(
            definitions=tuple(field for field in OFFER_FIELDS if field.section == "fields"),
            values=enriched_values,
            layout_key=OFFER_FIELDS_LAYOUT_KEY,
        )
        currency = self._text(values.get("currency")) or "EUR"
        notes_html = self.offer_notes(values)
        fields += (HubFinanceFieldValue(key="notes", label="Anmerkungen", display_type="HTML",
                                       value=unescape(re.sub(r"<[^>]+>", " ", notes_html)).strip(), form_value=notes_html),)
        lines = tuple(replace(self._line_view(line), currency=currency) for line in offer.lines)
        if offer.customer is not None:
            linked_kind = "Kunde"
            linked_href = f"/customers/{offer.customer.id}"
        elif lead_detail is not None:
            linked_kind = "Lead"
            linked_href = f"/leads/{lead_detail.lead.id}"
        else:
            linked_kind = ""
            linked_href = ""
        return FinanceOfferDetail(
            offer=offer,
            offer_number=self.offer_number(offer),
            status=self._display_option(self._field(OFFER_FIELDS, "status"), values.get("status")) or "-",
            linked_name=linked_name or "-",
            linked_kind=linked_kind,
            linked_href=linked_href,
            contact_name=contact_name,
            fields=fields,
            lines=lines,
            totals=replace(self._totals(lines), currency=currency),
            show_more_index=show_more_index,
            notes_html=notes_html,
        )

    def offer_notes(self, values: dict[str, str]) -> str:
        value = values["notes"] if "notes" in values else HubPdfTemplateService(db=self.db).offer_notes_default()
        return sanitize_offer_notes(value)

    @staticmethod
    def new_offer_values(*, offer_date: str | None = None) -> dict[str, str]:
        today = date.today()
        try:
            selected_date = date.fromisoformat(offer_date) if offer_date else today
        except ValueError:
            selected_date = today
        year = selected_date.year + (selected_date.month == 12)
        month = selected_date.month % 12 + 1
        valid_until = selected_date.replace(year=year, month=month, day=min(selected_date.day, monthrange(year, month)[1]))
        return {
            "offer_field__status": "draft",
            "offer_field__offer_date": selected_date.isoformat(),
            "offer_field__valid_until": valid_until.isoformat(),
            "offer_field__currency": "EUR",
            "offer_line__0__quantity": "1",
            "offer_line__0__tax_rate": "19",
            "offer_line__0__discount_percent": "0",
        }

    def create_offer(
        self,
        *,
        customer_id: int | None,
        contact_id: int | None,
        lead_id: int | None = None,
        submitted_values: dict[str, str],
        pdf_template_id: int | None = None,
    ) -> HubFinanceOffer:
        customer, contact, lead = self._linked_party(
            customer_id=customer_id,
            contact_id=contact_id,
            lead_id=lead_id,
        )
        pdf_template = self._pdf_template(pdf_template_id)
        values = self._submitted_offer_values(submitted_values)
        offer = HubFinanceOffer(
            customer=customer,
            contact=contact,
            lead=lead,
            pdf_template=pdf_template,
            encrypted_fields_json=self._encrypt(values),
        )
        self.db.add(offer)
        self.db.flush()
        offer.offer_number = f"ANG-{offer.id:06d}"
        self._replace_lines(offer=offer, submitted_values=submitted_values)
        self.db.flush()
        return offer

    def duplicate_offer(self, *, offer_id: int, owner_user_id: int) -> HubFinanceOffer:
        source = self.db.scalar(select(HubFinanceOffer).options(selectinload(HubFinanceOffer.lines)).where(HubFinanceOffer.id == offer_id))
        if source is None:
            raise HubFinanceError("Das Angebot wurde nicht gefunden.")
        values = self._values(source.encrypted_fields_json)
        values["notes"] = self.offer_notes(values)
        copied_values = {field.key: values.get(field.key, "") for field in OFFER_FIELDS
                         if not field.read_only and field.key not in {"customer", "contact"}}
        copied_values["status"] = self.new_offer_values()["offer_field__status"]
        # Temporary navigation metadata, removed by the first successful update.
        # The persisted owner and empty relations, never this ID, authorize discard.
        copied_values["_duplicate_source_offer_id"] = str(source.id)
        # Copy stored snapshots, not current article defaults or the original PDF.
        with self.db.begin_nested():
            offer = HubFinanceOffer(
                unassigned_owner_user_id=owner_user_id,
                pdf_template_id=source.pdf_template_id,
                encrypted_fields_json=self._encrypt(copied_values),
                lines=[HubFinanceOfferLine(article_id=line.article_id, position_index=line.position_index,
                        encrypted_fields_json=self._encrypt(self._values(line.encrypted_fields_json))) for line in source.lines],
            )
            self.db.add(offer)
            self.db.flush()
            offer.offer_number = f"ANG-{offer.id:06d}"
            self.db.flush()
        return offer

    def discard_offer_copy(self, *, offer_id: int, actor_user_id: int, is_admin: bool = False, after_commit=None) -> int | None:
        offer = self.db.scalar(select(HubFinanceOffer).where(HubFinanceOffer.id == offer_id)
                               .with_for_update().execution_options(populate_existing=True))
        if offer is None:
            raise HubFinanceError("Die Angebotskopie wurde nicht gefunden.")
        if (offer.unassigned_owner_user_id is None or offer.customer_id is not None
                or offer.lead_id is not None or offer.contact_id is not None):
            raise HubFinanceError("Das Angebot wurde bereits gespeichert und kann nicht mehr als Kopie verworfen werden.")
        if not is_admin and offer.unassigned_owner_user_id != actor_user_id:
            raise HubFinanceError("Diese Angebotskopie darf nicht verworfen werden.")
        source_id = self._values(offer.encrypted_fields_json).get("_duplicate_source_offer_id", "")
        self.delete_offer(offer_id=offer.id, after_commit=after_commit)
        return int(source_id) if source_id.isdecimal() and int(source_id) > 0 else None

    def update_offer(
        self,
        *,
        offer_id: int,
        customer_id: int | None,
        contact_id: int | None,
        lead_id: int | None = None,
        submitted_values: dict[str, str],
        pdf_template_id: int | None = None,
    ) -> HubFinanceOffer:
        offer = self.db.scalar(
            select(HubFinanceOffer).options(selectinload(HubFinanceOffer.lines)).where(HubFinanceOffer.id == offer_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if offer is None:
            raise HubFinanceError("Das Angebot wurde nicht gefunden.")
        customer, contact, lead = self._linked_party(
            customer_id=customer_id,
            contact_id=contact_id,
            lead_id=lead_id,
        )
        pdf_template = self._pdf_template(pdf_template_id)
        offer.customer = customer
        offer.contact = contact
        offer.lead = lead
        offer.unassigned_owner_user_id = None
        offer.pdf_template = pdf_template
        submitted_values = dict(submitted_values)
        submitted_values.setdefault("offer_field__notes", self.offer_notes(self._values(offer.encrypted_fields_json)))
        offer.encrypted_fields_json = self._encrypt(self._submitted_offer_values(submitted_values))
        self._replace_lines(offer=offer, submitted_values=submitted_values)
        self.db.flush()
        return offer

    def delete_offer(self, *, offer_id: int, after_commit=None) -> HubFinanceOffer:
        offer = self.db.get(HubFinanceOffer, offer_id)
        if offer is None:
            raise HubFinanceError("Das Angebot wurde nicht gefunden.")
        generated_pdf = self.db.scalar(
            select(HubFinanceGeneratedPdf).where(
                HubFinanceGeneratedPdf.document_type == "offers",
                HubFinanceGeneratedPdf.document_id == offer.id,
            )
        )
        if generated_pdf is not None:
            if generated_pdf.storage_key:
                storage = FinanceGeneratedPdfStorage(cipher=self.cipher)
                storage_key = generated_pdf.storage_key
                if after_commit is None:
                    storage.remove(storage_key)
                else:
                    after_commit(f"delete-finance-pdf:{storage_key}", lambda: storage.remove(storage_key))
            self.db.delete(generated_pdf)
        self.db.delete(offer)
        self.db.flush()
        return offer

    def _pdf_template(self, template_id: int | None):
        service = HubPdfTemplateService(db=self.db)
        template = service.get(template_id) if template_id else service.default_for("offers")
        if template is None or template.document_type != "offers":
            raise HubFinanceError("Die gewählte PDF-Vorlage passt nicht zum Angebot.")
        return template

    def list_linkable_customers(self) -> tuple[Customer, ...]:
        return tuple(self.db.scalars(select(Customer).where(Customer.is_visible.is_(True)).order_by(Customer.name.asc())).all())

    def list_linkable_contacts(self) -> tuple[FinanceContactOption, ...]:
        entries = CustomerDirectoryService(db=self.db, cipher=self.cipher).list_contact_entries()
        return tuple(
            FinanceContactOption(
                id=entry.contact.id,
                name=entry.contact.name,
                customer_id=entry.customer.id,
                customer_name=entry.customer.name,
            )
            for entry in entries if entry.customer is not None
        )

    def list_linkable_leads(self) -> tuple[FinanceLeadOption, ...]:
        return tuple(
            FinanceLeadOption(
                id=entry.lead.id,
                name=entry.name,
                company=entry.company,
                email=entry.email,
            )
            for entry in HubLeadService(db=self.db, cipher=self.cipher).list_leads()
        )

    def article_options(self) -> tuple[FinanceArticleEntry, ...]:
        return self.list_articles()

    @staticmethod
    def offer_number(offer: HubFinanceOffer) -> str:
        return offer.offer_number or f"ANG-{offer.id:06d}"

    @staticmethod
    def format_money(value: Decimal, currency: str = "EUR") -> str:
        amount = value.quantize(_CENT, rounding=ROUND_HALF_UP)
        grouped = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        symbol = "\u20ac" if currency == "EUR" else currency
        return f"{grouped} {symbol}".strip()

    @staticmethod
    def format_quantity(value: Decimal) -> str:
        normalized = value.quantize(_QUANTITY_STEP, rounding=ROUND_HALF_UP)
        return format(normalized, "f").replace(".", ",")

    def _offer_entry(self, offer: HubFinanceOffer) -> FinanceOfferEntry:
        values = self._values(offer.encrypted_fields_json)
        lines = tuple(self._line_view(line) for line in offer.lines)
        currency = self._text(values.get("currency")) or "EUR"
        lead_detail = self._lead_detail(offer.lead_id)
        if offer.customer is not None:
            linked_name = offer.customer.name
            linked_kind = "Kunde"
            linked_href = f"/customers/{offer.customer.id}"
        elif lead_detail is not None:
            linked_name = lead_detail.name
            linked_kind = "Lead"
            linked_href = f"/leads/{lead_detail.lead.id}"
        else:
            linked_name = "-"
            linked_kind = ""
            linked_href = ""
        return FinanceOfferEntry(
            offer=offer,
            offer_number=self.offer_number(offer),
            status=self._display_option(self._field(OFFER_FIELDS, "status"), values.get("status")) or "-",
            linked_name=linked_name,
            linked_kind=linked_kind,
            linked_href=linked_href,
            offer_date=self._display_date(self._text(values.get("offer_date"))) or "-",
            total_gross=self.format_money(self._totals(lines).total_gross, currency),
            currency=currency,
        )

    def _contact_name(self, contact_id: int | None) -> str:
        if contact_id is None:
            return ""
        return next(
            (entry.name for entry in self.list_linkable_contacts() if entry.id == contact_id),
            "",
        )

    def _field_values(
        self,
        *,
        definitions: tuple[HubFinanceField, ...],
        values: dict[str, str],
        layout_key: str,
    ) -> tuple[tuple[HubFinanceFieldValue, ...], int]:
        ordered_keys, show_more_index = ModuleLayoutService(db=self.db).ordered_keys_with_show_more(
            layout_key=layout_key,
            default_keys=tuple(field.key for field in definitions),
        )
        by_key = {field.key: field for field in definitions}
        fields = []
        for key in ordered_keys:
            definition = by_key[key]
            raw_value = self._text(values.get(key))
            value = raw_value
            if definition.display_type == "Auswahlliste":
                value = self._display_option(definition, raw_value)
            elif definition.display_type == "Waehrung":
                value = self.format_money(self._money(raw_value))
            elif definition.display_type == "Datum":
                value = self._display_date(raw_value)
            fields.append(HubFinanceFieldValue(
                key=definition.key,
                label=definition.label,
                display_type=definition.display_type,
                value=value,
                form_value=raw_value,
                required=definition.required,
                read_only=definition.read_only,
                options=definition.options,
            ))
        return tuple(fields), show_more_index

    def _submitted_article_values(self, submitted_values: dict[str, str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for definition in ARTICLE_FIELDS:
            raw = self._limited_text(submitted_values.get(f"article_field__{definition.key}"), definition.label)
            if definition.required and not raw:
                raise HubFinanceError(f"{definition.label} ist erforderlich.")
            if definition.key == "net_price":
                raw = self._decimal_text(raw, definition.label, minimum=Decimal("0"), places=_CENT)
            self._validate_option(definition, raw)
            values[definition.key] = raw
        return values

    def _submitted_offer_values(self, submitted_values: dict[str, str]) -> dict[str, str]:
        submitted_values = dict(submitted_values)
        if "offer_field__notes" not in submitted_values:
            submitted_values["offer_field__notes"] = self.offer_notes({})
        values: dict[str, str] = {}
        for definition in OFFER_FIELDS:
            if definition.read_only or definition.key in {"customer", "contact"}:
                continue
            raw = self._limited_text(submitted_values.get(f"offer_field__{definition.key}"), definition.label)
            if definition.display_type == "HTML":
                raw = sanitize_offer_notes(raw)
            if definition.required and not raw:
                raise HubFinanceError(f"{definition.label} ist erforderlich.")
            if definition.display_type == "Datum" and raw:
                try:
                    date.fromisoformat(raw)
                except ValueError as exc:
                    raise HubFinanceError(f"{definition.label} ist ungültig.") from exc
            self._validate_option(definition, raw)
            values[definition.key] = raw
        if values.get("valid_until") and values.get("offer_date") and values["valid_until"] < values["offer_date"]:
            raise HubFinanceError("Gültig bis darf nicht vor dem Angebotsdatum liegen.")
        return values

    def _replace_lines(self, *, offer: HubFinanceOffer, submitted_values: dict[str, str]) -> None:
        offer.lines.clear()
        self.db.flush()
        for index, values in enumerate(self._submitted_lines(submitted_values)):
            article_id = self._optional_id(values.pop("article_id", ""), "Artikel")
            article = self.db.get(HubFinanceArticle, article_id) if article_id is not None else None
            if article_id is not None and article is None:
                raise HubFinanceError("Der ausgewählte Artikel wurde nicht gefunden.")
            offer.lines.append(HubFinanceOfferLine(
                article=article,
                position_index=index,
                encrypted_fields_json=self._encrypt(values),
            ))

    def _submitted_lines(self, submitted_values: dict[str, str]) -> tuple[dict[str, str], ...]:
        raw_rows: dict[int, dict[str, str]] = {}
        for key, value in submitted_values.items():
            parts = key.split("__")
            if len(parts) != 3 or parts[0] != "offer_line" or not parts[1].isdigit():
                continue
            raw_rows.setdefault(int(parts[1]), {})[parts[2]] = str(value)
        lines = []
        for index in sorted(raw_rows):
            row = raw_rows[index]
            if self._truthy(row.get("delete")):
                continue
            article_id = self._limited_text(row.get("article_id"), "Artikel")
            name = self._limited_text(row.get("name"), "Position: Bezeichnung")
            description = self._limited_text(row.get("description"), "Position: Beschreibung")
            if not article_id and not name and not description:
                continue
            if not name:
                raise HubFinanceError("Jede Position benötigt eine Bezeichnung.")
            quantity = self._decimal_text(row.get("quantity"), "Position: Menge", minimum=Decimal("0.001"), places=_QUANTITY_STEP)
            unit = self._limited_text(row.get("unit"), "Position: Einheit")
            if unit and unit not in FINANCE_POSITION_UNITS:
                raise HubFinanceError("Die Auswahl für Position: Einheit ist ungültig.")
            unit_price = self._decimal_text(row.get("unit_price"), "Position: Einzelpreis netto", minimum=Decimal("0"), places=_CENT)
            discount = self._decimal_text(row.get("discount_percent"), "Position: Rabatt", minimum=Decimal("0"), maximum=Decimal("100"), places=_CENT)
            tax_rate = self._limited_text(row.get("tax_rate"), "Position: MwSt.-Satz")
            if tax_rate not in {"0", "7", "19"}:
                raise HubFinanceError("Die Auswahl für Position: MwSt.-Satz ist ungültig.")
            sku = self._limited_text(row.get("sku"), "Artikelnummer / SKU")
            lines.append({
                "article_id": article_id,
                "name": name,
                "sku": sku,
                "description": description,
                "quantity": quantity,
                "unit": unit,
                "unit_price": unit_price,
                "discount_percent": discount,
                "tax_rate": tax_rate,
            })
        return tuple(lines)

    def _linked_party(
        self,
        *,
        customer_id: int | None,
        contact_id: int | None,
        lead_id: int | None,
    ) -> tuple[Customer | None, CustomerContact | None, HubLead | None]:
        if (customer_id is None) == (lead_id is None):
            raise HubFinanceError("Bitte genau einen Kunden oder Lead auswählen.")
        if lead_id is not None:
            lead = self.db.get(HubLead, lead_id)
            if lead is None:
                raise HubFinanceError("Der ausgewählte Lead ist nicht verfügbar.")
            if contact_id is not None:
                raise HubFinanceError("Ein Ansprechpartner kann nur mit einem Kunden verknüpft werden.")
            return None, None, lead
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            raise HubFinanceError("Der ausgewählte Kunde ist nicht verfügbar.")
        if contact_id is None:
            raise HubFinanceError("Ansprechpartner ist erforderlich.")
        contact = self.db.get(CustomerContact, contact_id)
        if contact is None or contact.customer_id != customer.id:
            raise HubFinanceError("Der Ansprechpartner gehört nicht zum ausgewählten Kunden.")
        return customer, contact, None

    def _lead_detail(self, lead_id: int | None):
        if lead_id is None:
            return None
        return HubLeadService(db=self.db, cipher=self.cipher).get_detail(lead_id=lead_id)

    def _line_view(self, line: HubFinanceOfferLine) -> FinanceOfferLineView:
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
        net = raw_total - discount_value
        tax = (net * tax_rate / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)
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
            amount_net=net,
            tax_amount=tax,
            amount_gross=net + tax,
        )

    @staticmethod
    def _free_text_values(*, article_id: int | None, name: str, description: str) -> tuple[str, str]:
        if article_id is None and name.strip().casefold() in {"freitextposition", "freitext position"} and description.strip():
            return description, ""
        return name, description

    @staticmethod
    def _totals(lines: tuple[FinanceOfferLineView, ...]) -> FinanceOfferTotals:
        subtotal = Decimal("0")
        net_total = Decimal("0")
        tax_total = Decimal("0")
        for line in lines:
            raw_total = (HubFinanceService._quantity(line.quantity) * HubFinanceService._money(line.unit_price)).quantize(_CENT, rounding=ROUND_HALF_UP)
            subtotal += raw_total
            net_total += line.amount_net
            tax_total += line.tax_amount
        return FinanceOfferTotals(
            subtotal_net=subtotal.quantize(_CENT, rounding=ROUND_HALF_UP),
            discount_total=(subtotal - net_total).quantize(_CENT, rounding=ROUND_HALF_UP),
            tax_total=tax_total.quantize(_CENT, rounding=ROUND_HALF_UP),
            total_gross=(net_total + tax_total).quantize(_CENT, rounding=ROUND_HALF_UP),
        )

    def _values(self, encrypted_values: str) -> dict[str, str]:
        try:
            values = json.loads(self.cipher.decrypt(encrypted_values))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HubFinanceError("Die gespeicherten Finanzdaten sind ungültig.") from exc
        return {key: self._text(value) for key, value in values.items()} if isinstance(values, dict) else {}

    def _encrypt(self, values: dict[str, str]) -> str:
        return self.cipher.encrypt(json.dumps(values, ensure_ascii=False, separators=(",", ":")))

    @classmethod
    def _limited_text(cls, raw_value: object, label: str) -> str:
        value = cls._text(raw_value).strip()
        if len(value) > cls._MAX_TEXT_LENGTH:
            raise HubFinanceError(f"{label} ist zu lang.")
        return value

    @staticmethod
    def _text(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _field(definitions: tuple[HubFinanceField, ...], key: str) -> HubFinanceField:
        return next(field for field in definitions if field.key == key)

    @staticmethod
    def _validate_option(definition: HubFinanceField, value: str) -> None:
        if value and definition.options and value not in {option for option, _label in definition.options}:
            raise HubFinanceError(f"Die Auswahl für {definition.label} ist ungültig.")

    @staticmethod
    def _display_option(definition: HubFinanceField, value: object) -> str:
        text = HubFinanceService._text(value)
        return next((label for option, label in definition.options if option == text), text)

    @staticmethod
    def _decimal_text(
        raw_value: object,
        label: str,
        *,
        minimum: Decimal,
        places: Decimal,
        maximum: Decimal | None = None,
    ) -> str:
        value = HubFinanceService._decimal(raw_value, label)
        if value < minimum or (maximum is not None and value > maximum):
            raise HubFinanceError(f"{label} ist ungültig.")
        return format(value.quantize(places, rounding=ROUND_HALF_UP), "f")

    @staticmethod
    def _decimal(raw_value: object, label: str) -> Decimal:
        text = HubFinanceService._text(raw_value).strip().replace(" ", "").replace(",", ".")
        if not text:
            raise HubFinanceError(f"{label} ist erforderlich.")
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise HubFinanceError(f"{label} ist ungültig.") from exc

    @staticmethod
    def _money(raw_value: object) -> Decimal:
        try:
            return Decimal(HubFinanceService._text(raw_value).replace(",", ".") or "0")
        except InvalidOperation:
            return Decimal("0")

    @staticmethod
    def _quantity(raw_value: object) -> Decimal:
        try:
            return Decimal(HubFinanceService._text(raw_value).replace(",", ".") or "0")
        except InvalidOperation:
            return Decimal("0")

    @staticmethod
    def _percentage(raw_value: object) -> Decimal:
        value = HubFinanceService._money(raw_value)
        return value if Decimal("0") <= value <= Decimal("100") else Decimal("0")

    @staticmethod
    def _optional_id(value: str, label: str) -> int | None:
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise HubFinanceError(f"{label} ist ungültig.") from exc

    @staticmethod
    def _truthy(value: object) -> bool:
        return HubFinanceService._text(value).casefold() in {"true", "1", "on", "yes"}

    @staticmethod
    def _display_date(value: str) -> str:
        try:
            return date.fromisoformat(value).strftime("%d.%m.%Y")
        except ValueError:
            return value
