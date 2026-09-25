"""Background generation of versioned Finance PDFs and ZUGFeRD invoices."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from html import escape
import json
import logging
from pathlib import Path
import re
from secrets import token_urlsafe
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import SessionLocal
from app.models.hub_finance_documents import HubFinanceDunning, HubFinanceInvoice, HubFinanceOrder
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_finance_offer import HubFinanceOffer
from app.models.hub_legal_terms import HubLegalTermsRevision
from app.models.hub_pdf_template import HubPdfTemplate
from app.services.customer_directory import CustomerDirectoryDetail, CustomerDirectoryService
from app.services.customer_profile import resolve_customer_fields
from app.services.email_compose_images import EmailComposeImageError, EmailComposeImageService
from app.services.finance_generated_pdf_storage import FinanceGeneratedPdfStorage
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import DUNNING_MODULE, INVOICE_MODULE, ORDER_MODULE, HubFinanceDocumentService
from app.services.hub_leads import HubLeadService
from app.services.hub_pdf_templates import HubPdfTemplateError, HubPdfTemplateService
from app.services.hub_offer_notes import OFFER_NOTES_TOKEN, sanitize_offer_notes
from app.services.template_placeholders import DOCUMENT_NAMES, INVOICE_CUSTOMER_BANK_PLACEHOLDERS, contact_greeting, pdf_document_placeholders, profile_placeholders
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS
from app.services.zoho_contact_field_catalog import ZOHO_CONTACT_FIELDS


logger = logging.getLogger(__name__)
_CENT = Decimal("0.01")
_PLACEHOLDER_PATTERN = re.compile(r"\$\{[A-Za-z][A-Za-z0-9.]*\}")
_DOCUMENT_MODELS = {
    "offers": HubFinanceOffer,
    "orders": HubFinanceOrder,
    "invoices": HubFinanceInvoice,
    "dunnings": HubFinanceDunning,
}
_PDF_TEMPLATE_ENV = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
    autoescape=select_autoescape(("html", "xml")),
)


class HubFinancePdfError(ValueError):
    """A safe PDF generation or retrieval error."""


@dataclass(frozen=True)
class FinanceGeneratedPdfView:
    status: str
    filename: str
    byte_size: int
    template_name: str
    template_version: int
    is_zugferd: bool
    zugferd_version: str
    zugferd_profile: str
    validation_status: str
    error_message: str
    generated_at: datetime | None

    @property
    def is_pending(self) -> bool:
        return self.status in {"queued", "rendering"}

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True)
class FinancePdfSnapshot:
    document_type: str
    identifier: str
    document_title: str
    document_status: str
    document_date: str
    due_date: str
    source_invoice_number: str
    valid_until: str
    payment_terms: str
    currency: str
    customer_name: str
    contact_name: str
    billing_street: str
    billing_postal_code: str
    billing_city: str
    billing_country_code: str
    lines: tuple[Any, ...]
    totals: Any
    customer_fields: dict[str, str] = field(default_factory=dict)
    contact_fields: dict[str, str] = field(default_factory=dict)
    notes_html: str = ""
    document_fields: dict[str, str] = field(default_factory=dict)


class HubFinancePdfService:
    """Queue, render and retrieve the latest generated Finance document PDF."""

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def queue(self, *, document_type: str, document_id: int) -> str:
        model = self._model(document_type)
        document = self.db.get(model, document_id)
        if document is None:
            raise HubFinancePdfError("Der Beleg wurde nicht gefunden.")
        template_service = HubPdfTemplateService(db=self.db)
        template = self._template_for(document=document, document_type=document_type, service=template_service)
        revision = template_service.revision_for(template)
        generation_token = token_urlsafe(32)
        now = datetime.now(UTC)
        generated_pdf = self.db.scalar(
            select(HubFinanceGeneratedPdf).where(
                HubFinanceGeneratedPdf.document_type == document_type,
                HubFinanceGeneratedPdf.document_id == document_id,
            )
        )
        if generated_pdf is None:
            generated_pdf = HubFinanceGeneratedPdf(
                document_type=document_type,
                document_id=document_id,
                template_name=template.name,
                template_version=template.version,
                generation_token=generation_token,
                requested_at=now,
            )
            self.db.add(generated_pdf)
        generated_pdf.template_id = template.id
        generated_pdf.template_revision_id = revision.id
        generated_pdf.template_name = template.name
        generated_pdf.template_version = template.version
        generated_pdf.status = "queued"
        generated_pdf.generation_token = generation_token
        generated_pdf.is_zugferd = document_type == "invoices"
        generated_pdf.zugferd_version = "2.5.2" if document_type == "invoices" else None
        generated_pdf.zugferd_profile = "EN 16931" if document_type == "invoices" else None
        generated_pdf.validation_status = "pending" if document_type == "invoices" else "not-applicable"
        generated_pdf.error_message = None
        generated_pdf.requested_at = now
        generated_pdf.generation_started_at = None
        generated_pdf.generated_at = None
        generated_pdf.attempt_count = 0
        generated_pdf.next_retry_at = None
        self.db.flush()
        return generation_token

    def view(self, *, document_type: str, document_id: int) -> FinanceGeneratedPdfView | None:
        self._model(document_type)
        generated_pdf = self.db.scalar(
            select(HubFinanceGeneratedPdf).where(
                HubFinanceGeneratedPdf.document_type == document_type,
                HubFinanceGeneratedPdf.document_id == document_id,
            )
        )
        if generated_pdf is None:
            return None
        return FinanceGeneratedPdfView(
            status=generated_pdf.status,
            filename=generated_pdf.filename or "",
            byte_size=generated_pdf.byte_size or 0,
            template_name=generated_pdf.template_name,
            template_version=generated_pdf.template_version,
            is_zugferd=generated_pdf.is_zugferd,
            zugferd_version=generated_pdf.zugferd_version or "",
            zugferd_profile=generated_pdf.zugferd_profile or "",
            validation_status=generated_pdf.validation_status,
            error_message=generated_pdf.error_message or "",
            generated_at=generated_pdf.generated_at,
        )

    def load(self, *, document_type: str, document_id: int) -> tuple[HubFinanceGeneratedPdf, bytes]:
        self._model(document_type)
        generated_pdf = self.db.scalar(
            select(HubFinanceGeneratedPdf).where(
                HubFinanceGeneratedPdf.document_type == document_type,
                HubFinanceGeneratedPdf.document_id == document_id,
            )
        )
        if generated_pdf is None or generated_pdf.status != "ready" or not generated_pdf.storage_key:
            raise HubFinancePdfError("Für diesen Beleg ist noch keine erzeugte PDF verfügbar.")
        return generated_pdf, FinanceGeneratedPdfStorage(cipher=self.cipher).load(generated_pdf.storage_key)

    def generate(self, *, generation_token: str) -> None:
        now = datetime.now(UTC)
        claim = self.db.execute(
            update(HubFinanceGeneratedPdf)
            .where(
                HubFinanceGeneratedPdf.generation_token == generation_token,
                HubFinanceGeneratedPdf.status == "queued",
                or_(HubFinanceGeneratedPdf.next_retry_at.is_(None), HubFinanceGeneratedPdf.next_retry_at <= now),
            )
            .values(
                status="rendering",
                generation_started_at=now,
                attempt_count=HubFinanceGeneratedPdf.attempt_count + 1,
            )
        )
        if claim.rowcount != 1:
            self.db.rollback()
            return
        self.db.commit()
        generated_pdf = self.db.scalar(
            select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.generation_token == generation_token)
        )
        if generated_pdf is None:
            return

        template = self.db.get(HubPdfTemplate, generated_pdf.template_id)
        if template is None:
            raise HubFinancePdfError("Die für den Beleg gewählte PDF-Vorlage wurde gelöscht.")
        revision = HubPdfTemplateService(db=self.db).revision_for(template)
        if revision.id != generated_pdf.template_revision_id:
            revision = self.db.get(type(revision), generated_pdf.template_revision_id)
        if revision is None:
            raise HubFinancePdfError("Die verwendete Vorlagenversion wurde nicht gefunden.")

        snapshot = self._snapshot(
            document_type=generated_pdf.document_type,
            document_id=generated_pdf.document_id,
        )
        content = HubPdfTemplateService(db=self.db).decoded_content(
            document_type=generated_pdf.document_type,
            content_json=revision.content_json,
        )
        legal_terms_html = ""
        if generated_pdf.document_type in {"offers", "orders"} and revision.legal_terms_revision_id is not None:
            legal_terms_revision = self.db.get(HubLegalTermsRevision, revision.legal_terms_revision_id)
            if legal_terms_revision is not None:
                legal_terms_html = legal_terms_revision.content_html
        html = self._render_html(
            snapshot=snapshot,
            content=content,
            legal_terms_html=legal_terms_html,
        )
        html = EmailComposeImageService(db=self.db, cipher=self.cipher).embed_local_images(html)
        pdf_content = self._render_pdf(html=html, snapshot=snapshot)
        validation_status = "not-applicable"
        if generated_pdf.document_type == "invoices":
            pdf_content = self._add_zugferd(pdf_content=pdf_content, snapshot=snapshot)
            validation_status = "xsd-valid-draft"

        self.db.expire_all()
        latest = self.db.scalar(
            select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.id == generated_pdf.id)
        )
        if latest is None or latest.generation_token != generation_token:
            return
        storage = FinanceGeneratedPdfStorage(cipher=self.cipher)
        storage_key = storage.store(pdf_content)
        old_storage_key = latest.storage_key
        latest.storage_key = storage_key
        latest.filename = f"{self._filename_stem(snapshot.identifier)}.pdf"
        latest.content_type = "application/pdf"
        latest.byte_size = len(pdf_content)
        latest.status = "ready"
        latest.validation_status = validation_status
        latest.error_message = None
        latest.generated_at = datetime.now(UTC)
        latest.next_retry_at = None
        self.db.commit()
        if old_storage_key and old_storage_key != storage_key:
            storage.remove(old_storage_key)

    def mark_failed(self, *, generation_token: str, error: Exception) -> None:
        generated_pdf = self.db.scalar(
            select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.generation_token == generation_token)
        )
        if generated_pdf is None:
            return
        retry = generated_pdf.attempt_count < 3
        generated_pdf.status = "queued" if retry else "failed"
        generated_pdf.validation_status = "pending" if retry and generated_pdf.is_zugferd else "failed" if generated_pdf.is_zugferd else "not-applicable"
        generated_pdf.error_message = self._safe_error(error)
        generated_pdf.generated_at = datetime.now(UTC)
        generated_pdf.next_retry_at = datetime.now(UTC) + timedelta(minutes=5 * generated_pdf.attempt_count) if retry else None
        self.db.commit()

    def _snapshot(self, *, document_type: str, document_id: int) -> FinancePdfSnapshot:
        if document_type == "offers":
            service = HubFinanceService(db=self.db, cipher=self.cipher)
            detail = service.get_offer_detail(offer_id=document_id)
            if detail is None:
                raise HubFinancePdfError("Das Angebot wurde nicht gefunden.")
            fields = {field.key: field for field in detail.fields}
            document = detail.offer
            date_key = "offer_date"
            title = "Angebot"
            due_date = ""
            valid_until = self._field_form_value(fields, "valid_until")
        else:
            module = {
                "orders": ORDER_MODULE,
                "invoices": INVOICE_MODULE,
                "dunnings": DUNNING_MODULE,
            }.get(document_type)
            if module is None:
                raise HubFinancePdfError("Für diese Belegart kann keine PDF erzeugt werden.")
            service = HubFinanceDocumentService(db=self.db, cipher=self.cipher)
            detail = service.get_detail(module=module, document_id=document_id)
            if detail is None:
                raise HubFinancePdfError("Der Beleg wurde nicht gefunden.")
            fields = {field.key: field for field in detail.fields}
            document = detail.document
            date_key = module.date_key
            title = {
                "orders": self._field_form_value(fields, "order_name"),
                "invoices": "Rechnung",
                "dunnings": "Mahnung",
            }[document_type]
            due_date = self._field_form_value(fields, "due_date") if document_type in {"invoices", "dunnings"} else ""
            valid_until = ""
        directory = CustomerDirectoryService(db=self.db, cipher=self.cipher)
        customer_detail = directory.get_detail(customer_id=document.customer_id) if document.customer_id is not None else None
        customer_address = self._customer_address(customer_detail)
        lead_detail = None
        if document_type == "offers" and getattr(document, "lead_id", None) is not None:
            lead_detail = HubLeadService(db=self.db, cipher=self.cipher).get_detail(lead_id=document.lead_id)
        if document_type in {"invoices", "dunnings"} and getattr(detail, "billing_address", ""):
            customer_address = self._address_with_snapshot_fallback(
                customer_address,
                detail.billing_address,
            )
        customer_fields: dict[str, str] = {}
        contact_fields: dict[str, str] = {}
        if customer_detail is not None:
            customer_labels = {field.label: field.key for field in ZOHO_ACCOUNT_FIELDS}
            customer_fields = {
                customer_labels.get(field.label, "website" if field.label == "Webseite" else field.key): field.value or ""
                for field in customer_detail.profile_fields
            }
            if document_type == "invoices":
                # Invoice output explicitly allows these bank fields, not other protected CRM data.
                bank_keys = {item.profile_key for item in INVOICE_CUSTOMER_BANK_PLACEHOLDERS}
                customer_fields.update({
                    field.key: field.value.strip() if isinstance(field.value, str) else ""
                    for field in resolve_customer_fields(directory._profile_data(customer_detail.entry.customer))
                    if field.key in bank_keys
                })
        if document.customer_id is not None and document.contact_id is not None:
            contact_detail = directory.get_contact_detail(customer_id=document.customer_id, contact_id=document.contact_id)
            if contact_detail is not None:
                contact_labels = {field.label: field.key for field in ZOHO_CONTACT_FIELDS}
                contact_fields = {
                    contact_labels.get(field.label, field.key): field.value or ""
                    for field in contact_detail.profile_fields
                }
        customer_name = document.customer.name if document.customer is not None else ""
        contact_name = detail.contact_name
        if lead_detail is not None:
            lead_values = {
                field.key: field.form_value
                for field in lead_detail.fields
                if isinstance(field.form_value, str)
            }
            customer_name = lead_values.get("company", "") or lead_detail.name
            contact_name = lead_detail.name
            customer_address = (
                lead_values.get("street", ""),
                lead_values.get("postal_code", ""),
                lead_values.get("city", ""),
                self._country_code(lead_values.get("country", "")),
            )
            customer_fields = {
                "billing_street": customer_address[0],
                "billing_postal_code": customer_address[1],
                "billing_city": customer_address[2],
                "phone": lead_values.get("phone", ""),
                "website": lead_values.get("website", ""),
            }
            contact_fields = {
                "salutation": lead_values.get("salutation", ""),
                "first_name": lead_values.get("first_name", ""),
                "last_name": lead_values.get("last_name", ""),
                "email": lead_values.get("email", ""),
                "phone": lead_values.get("phone", ""),
                "mailing_street": customer_address[0],
                "mailing_postal_code": customer_address[1],
                "mailing_city": customer_address[2],
            }
        return FinancePdfSnapshot(
            document_type=document_type,
            identifier=detail.offer_number if document_type == "offers" else detail.identifier,
            document_title=title or {"offers": "Angebot", "orders": "Auftrag", "invoices": "Rechnung", "dunnings": "Mahnung"}[document_type],
            document_status=detail.status,
            document_date=self._field_form_value(fields, date_key),
            due_date=due_date,
            source_invoice_number=detail.link_label if document_type == "dunnings" else "",
            valid_until=valid_until,
            payment_terms=self._field_display_value(fields, "payment_terms"),
            currency=self._field_form_value(fields, "currency") or "EUR",
            customer_name=customer_name,
            contact_name=contact_name,
            billing_street=customer_address[0],
            billing_postal_code=customer_address[1],
            billing_city=customer_address[2],
            billing_country_code=customer_address[3],
            lines=detail.lines,
            totals=detail.totals,
            customer_fields=customer_fields,
            contact_fields=contact_fields,
            notes_html=detail.notes_html if document_type == "offers" else "",
            document_fields={key: self._field_display_value(fields, key) for key in fields} if document_type == "orders" else {},
        )

    def _render_html(
        self,
        *,
        snapshot: FinancePdfSnapshot,
        content: dict[str, object],
        legal_terms_html: str = "",
    ) -> str:
        settings = get_settings()
        replacements = {
            "${companyName}": settings.finance_company_name,
            "${companyStreet}": settings.finance_company_street,
            "${companyPostalCode}": settings.finance_company_postal_code,
            "${companyCity}": settings.finance_company_city,
            "${companyPhone}": settings.finance_company_phone,
            "${companyEmail}": settings.finance_company_email,
            "${companyIban}": settings.finance_company_iban,
            "${companyBic}": settings.finance_company_bic,
            "${companyTaxId}": settings.finance_company_tax_id,
            "${customerName}": snapshot.customer_name,
            "${contactName}": snapshot.contact_name,
            "${billingStreet}": snapshot.billing_street,
            "${billingPostalCode}": snapshot.billing_postal_code,
            "${billingCity}": snapshot.billing_city,
            "${documentTitle}": snapshot.document_title,
            "${documentNumber}": snapshot.identifier,
            "${documentDate}": self._display_date(snapshot.document_date),
            "${validUntil}": self._display_date(snapshot.valid_until),
            "${dueDate}": self._display_date(snapshot.due_date),
            "${paymentTerms}": snapshot.payment_terms,
            "${netTotal}": self._money(snapshot.totals.subtotal_net, snapshot.currency),
            "${taxTotal}": self._money(snapshot.totals.tax_total, snapshot.currency),
            "${grossTotal}": self._money(snapshot.totals.total_gross, snapshot.currency),
        }
        namespace = DOCUMENT_NAMES[snapshot.document_type][0]
        replacements.update({
            "${Company.Name}": settings.finance_company_name,
            "${Company.Street}": settings.finance_company_street,
            "${Company.PostalCode}": settings.finance_company_postal_code,
            "${Company.City}": settings.finance_company_city,
            "${Company.Phone}": settings.finance_company_phone,
            "${Company.Email}": settings.finance_company_email,
            "${Company.Iban}": settings.finance_company_iban,
            "${Company.Bic}": settings.finance_company_bic,
            "${Company.TaxId}": settings.finance_company_tax_id,
            "${Customer.Name}": snapshot.customer_name,
            "${Customer.BillingStreet}": snapshot.billing_street,
            "${Customer.BillingPostalCode}": snapshot.billing_postal_code,
            "${Customer.BillingCity}": snapshot.billing_city,
            "${Contact.Name}": snapshot.contact_name,
            "${Contact.Greeting}": contact_greeting(
                name=snapshot.contact_name,
                salutation=snapshot.contact_fields.get("salutation", ""),
                last_name=snapshot.contact_fields.get("last_name", ""),
                letter_salutation=snapshot.contact_fields.get("letter_salutation", ""),
            ),
            f"${{{namespace}.Number}}": snapshot.identifier,
            f"${{{namespace}.Title}}": snapshot.document_title,
            f"${{{namespace}.Status}}": snapshot.document_status,
            f"${{{namespace}.Date}}": self._display_date(snapshot.document_date),
            f"${{{namespace}.DueDate}}": self._display_date(snapshot.due_date),
            f"${{{namespace}.SourceInvoiceNumber}}": snapshot.source_invoice_number,
            f"${{{namespace}.ValidUntil}}": self._display_date(snapshot.valid_until),
            f"${{{namespace}.PaymentTerms}}": snapshot.payment_terms,
            f"${{{namespace}.NetTotal}}": self._money(snapshot.totals.subtotal_net, snapshot.currency),
            f"${{{namespace}.TaxTotal}}": self._money(snapshot.totals.tax_total, snapshot.currency),
            f"${{{namespace}.GrossTotal}}": self._money(snapshot.totals.total_gross, snapshot.currency),
        })
        for placeholder in pdf_document_placeholders(snapshot.document_type):
            if placeholder.profile_key:
                replacements.setdefault(placeholder.token, snapshot.document_fields.get(placeholder.profile_key, ""))
        replacements.update(profile_placeholders("Customer", snapshot.customer_fields, document_type=snapshot.document_type))
        replacements.update(profile_placeholders("Contact", snapshot.contact_fields))
        replacements["${Customer.BillingStreet}"] = snapshot.billing_street
        replacements["${Customer.BillingPostalCode}"] = snapshot.billing_postal_code
        replacements["${Customer.BillingCity}"] = snapshot.billing_city
        rendered_blocks: dict[str, str] = {}
        # Resolve embedded greeting/field tokens once; record text is never markup.
        notes_html = ""
        if snapshot.document_type == "offers":
            notes_html = sanitize_offer_notes(snapshot.notes_html)
            notes_html = _PLACEHOLDER_PATTERN.sub(lambda match: escape(replacements.get(match.group(0), "") or ""), notes_html)
            notes_html = sanitize_offer_notes(notes_html)
        blocks = content.get("blocks", {})
        for key, raw in blocks.items() if isinstance(blocks, dict) else ():
            if not isinstance(raw, dict) or not raw.get("is_visible", True):
                rendered_blocks[str(key)] = ""
                continue
            rendered = str(raw.get("content_html", ""))
            rendered_blocks[str(key)] = _PLACEHOLDER_PATTERN.sub(
                lambda match: notes_html if match.group(0) == OFFER_NOTES_TOKEN else escape(replacements.get(match.group(0), "") or ""),
                rendered,
            )
        rendered_blocks["legal"] = (
            _PLACEHOLDER_PATTERN.sub(lambda match: escape(replacements.get(match.group(0), "") or ""), legal_terms_html)
            if snapshot.document_type in {"offers", "orders"}
            else ""
        )
        positions = content.get("positions", {})
        columns = positions.get("columns", []) if isinstance(positions, dict) else []
        visible_columns = tuple(column for column in columns if isinstance(column, dict) and column.get("is_enabled"))
        rows = tuple(self._line_row(index, line, snapshot.currency) for index, line in enumerate(snapshot.lines, start=1))
        return _PDF_TEMPLATE_ENV.get_template("finance_generated_pdf.html").render(
            blocks=rendered_blocks,
            columns=visible_columns,
            show_totals=positions.get("show_totals") is not False if isinstance(positions, dict) else True,
            rows=rows,
            currency=snapshot.currency,
            subtotal=self._money(snapshot.totals.subtotal_net, snapshot.currency),
            discount=self._money(snapshot.totals.discount_total, snapshot.currency),
            tax=self._money(snapshot.totals.tax_total, snapshot.currency),
            gross=self._money(snapshot.totals.total_gross, snapshot.currency),
            has_legal=bool(rendered_blocks.get("legal")),
        )

    @staticmethod
    def _render_pdf(*, html: str, snapshot: FinancePdfSnapshot) -> bytes:
        try:
            from weasyprint import HTML
        except (ImportError, OSError) as exc:
            raise HubFinancePdfError("Der PDF-Renderer ist auf dem Server nicht vollständig installiert.") from exc
        variant = "pdf/a-3u" if snapshot.document_type == "invoices" else "pdf/a-2u"
        result = HTML(string=html).write_pdf(
            pdf_variant=variant,
            pdf_identifier=True,
            custom_metadata=True,
        )
        if not isinstance(result, bytes) or not result.startswith(b"%PDF-"):
            raise HubFinancePdfError("Der PDF-Renderer hat keine gültige PDF erzeugt.")
        return result

    @staticmethod
    def _add_zugferd(*, pdf_content: bytes, snapshot: FinancePdfSnapshot) -> bytes:
        try:
            from facturx import generate_from_binary, generate_xml, get_facturx_xml_from_pdf, xml_check_xsd
        except ImportError as exc:
            raise HubFinancePdfError("Die ZUGFeRD-Bibliothek ist auf dem Server nicht installiert.") from exc
        data = HubFinancePdfService._zugferd_data(snapshot)
        xml = generate_xml(
            data,
            flavor="factur-x",
            level="en16931",
            check_xsd=True,
            check_schematron=False,
        )
        result = generate_from_binary(
            pdf_content,
            xml,
            flavor="factur-x",
            level="en16931",
            check_xsd=True,
            check_schematron=False,
            lang="de-DE",
            afrelationship="data",
            pdf_metadata={
                "author": get_settings().finance_company_name,
                "title": f"Rechnung {snapshot.identifier}",
                "subject": "ZUGFeRD-Rechnung (technischer Entwurf)",
                "keywords": "ZUGFeRD, Factur-X, EN 16931",
            },
        )
        embedded = get_facturx_xml_from_pdf(result)
        if not embedded:
            raise HubFinancePdfError("Die ZUGFeRD-XML wurde nicht in die PDF eingebettet.")
        _, embedded_xml = embedded
        xml_check_xsd(embedded_xml, flavor="factur-x", level="en16931")
        return result

    @staticmethod
    def _zugferd_data(snapshot: FinancePdfSnapshot) -> dict[str, object]:
        settings = get_settings()
        invoice_date = HubFinancePdfService._required_date(snapshot.document_date, "Rechnungsdatum")
        due_date = HubFinancePdfService._optional_date(snapshot.due_date)
        tax_groups: dict[Decimal, tuple[Decimal, Decimal]] = {}
        lines: list[dict[str, object]] = []
        for index, line in enumerate(snapshot.lines, start=1):
            quantity = HubFinancePdfService._decimal(line.quantity, Decimal("1"))
            tax_rate = HubFinancePdfService._decimal(line.tax_rate, Decimal("0"))
            net_unit_price = (line.amount_net / quantity).quantize(_CENT, rounding=ROUND_HALF_UP)
            lines.append({
                "BT-126": str(index),
                "BT-153": line.name or f"Position {index}",
                "BT-154": line.description or None,
                "BT-155": line.sku or None,
                "BT-146": net_unit_price,
                "BT-129": quantity,
                "BT-130": "C62",
                "BT-151": "S" if tax_rate else "Z",
                "BT-152": tax_rate,
                "BT-131": line.amount_net.quantize(_CENT, rounding=ROUND_HALF_UP),
            })
            taxable, tax = tax_groups.get(tax_rate, (Decimal("0"), Decimal("0")))
            tax_groups[tax_rate] = (taxable + line.amount_net, tax + line.tax_amount)
        tax_breakdown = [
            {
                "BT-116": taxable.quantize(_CENT, rounding=ROUND_HALF_UP),
                "BT-117": tax.quantize(_CENT, rounding=ROUND_HALF_UP),
                "BT-118": "S" if rate else "Z",
                "BT-119": rate,
            }
            for rate, (taxable, tax) in sorted(tax_groups.items())
        ]
        data: dict[str, object] = {
            "BT-1": snapshot.identifier,
            "BT-2": invoice_date,
            "BT-3": "380",
            "BT-5": snapshot.currency,
            "BT-27": settings.finance_company_name,
            "BT-31": settings.finance_company_tax_id,
            "BT-35": settings.finance_company_street,
            "BT-37": settings.finance_company_city,
            "BT-38": settings.finance_company_postal_code,
            "BT-40": settings.finance_company_country_code,
            "BT-44": snapshot.customer_name or "Unbekannter Rechnungsempfänger",
            "BT-50": snapshot.billing_street or None,
            "BT-52": snapshot.billing_city or None,
            "BT-53": snapshot.billing_postal_code or None,
            "BT-55": snapshot.billing_country_code or "DE",
            "BT-72": invoice_date,
            "BT-106": sum((line.amount_net for line in snapshot.lines), Decimal("0")).quantize(_CENT, rounding=ROUND_HALF_UP),
            "BT-109": (snapshot.totals.subtotal_net - snapshot.totals.discount_total).quantize(_CENT, rounding=ROUND_HALF_UP),
            "BT-110": snapshot.totals.tax_total.quantize(_CENT, rounding=ROUND_HALF_UP),
            "BT-110-1": snapshot.currency,
            "BT-112": snapshot.totals.total_gross.quantize(_CENT, rounding=ROUND_HALF_UP),
            "BT-115": snapshot.totals.total_gross.quantize(_CENT, rounding=ROUND_HALF_UP),
            "BG-23": tax_breakdown,
            "BG-25": lines,
        }
        if due_date:
            data["BT-9"] = due_date
        if snapshot.payment_terms:
            data["BT-20"] = snapshot.payment_terms
        return {key: value for key, value in data.items() if value is not None}

    def _customer_address(self, detail: CustomerDirectoryDetail | None) -> tuple[str, str, str, str]:
        if detail is None:
            return "", "", "", "DE"
        by_key = {field.key: field.value or "" for field in detail.profile_fields}
        by_label = {field.label.casefold(): field.value or "" for field in detail.profile_fields}
        def value(key: str, *labels: str) -> str:
            direct = by_key.get(key, "")
            if direct:
                return direct
            for label in labels:
                candidate = by_label.get(label.casefold(), "")
                if candidate:
                    return candidate
            return ""
        return (
            value("billing_street", "Rechnungsadresse - Straße", "Rechnungsadresse - Straße Einzelzeile"),
            value("billing_postal_code", "Rechnungsadresse - PLZ"),
            value("billing_city", "Rechnungsadresse - Stadt"),
            self._country_code(value("billing_country", "Rechnungsadresse - Land")),
        )

    @staticmethod
    def _address_with_snapshot_fallback(
        address: tuple[str, str, str, str],
        billing_address: str,
    ) -> tuple[str, str, str, str]:
        street, postal_code, city, country_code = address
        lines = [line.strip() for line in billing_address.splitlines() if line.strip()]
        address_lines = lines[1:] if len(lines) > 1 else lines
        if not street and address_lines:
            street = address_lines[0]
        if (not postal_code or not city) and len(address_lines) > 1:
            match = re.match(r"^(\d{4,10})\s+(.+)$", address_lines[1])
            if match:
                postal_code = postal_code or match.group(1)
                city = city or match.group(2)
        return street, postal_code, city, country_code

    @staticmethod
    def _template_for(*, document: Any, document_type: str, service: HubPdfTemplateService) -> HubPdfTemplate:
        template_id = getattr(document, "pdf_template_id", None)
        template = service.get(template_id) if template_id else service.default_for(document_type)
        if template is None or template.document_type != document_type:
            raise HubFinancePdfError("Die gewählte PDF-Vorlage passt nicht zur Belegart.")
        if not template_id:
            document.pdf_template_id = template.id
        return template

    @staticmethod
    def validate_template(*, db: Session, document_type: str, template_id: int | None) -> HubPdfTemplate:
        service = HubPdfTemplateService(db=db)
        template = service.get(template_id) if template_id else service.default_for(document_type)
        if template is None or template.document_type != document_type:
            raise HubFinancePdfError("Die gewählte PDF-Vorlage passt nicht zur Belegart.")
        return template

    @staticmethod
    def _model(document_type: str) -> type[Any]:
        model = _DOCUMENT_MODELS.get(document_type)
        if model is None:
            raise HubFinancePdfError("Für diese Belegart kann keine PDF erzeugt werden.")
        return model

    @staticmethod
    def _field_form_value(fields: dict[str, Any], key: str) -> str:
        field = fields.get(key)
        return str(field.form_value or "") if field is not None else ""

    @staticmethod
    def _field_display_value(fields: dict[str, Any], key: str) -> str:
        field = fields.get(key)
        return str(field.value or "") if field is not None else ""

    @staticmethod
    def _line_row(index: int, line: Any, currency: str) -> dict[str, str]:
        return {
            "position": str(index),
            "article_name": line.name,
            "article_description": line.description,
            "sku": line.sku,
            "quantity": HubFinancePdfService._display_decimal(line.quantity),
            "unit": line.unit,
            "unit_price": HubFinancePdfService._money(HubFinancePdfService._decimal(line.unit_price), currency),
            "tax_rate": f"{HubFinancePdfService._display_decimal(line.tax_rate)} %",
            "line_total": HubFinancePdfService._money(line.amount_net, currency),
        }

    @staticmethod
    def _display_decimal(value: object) -> str:
        text = str(value or "0").replace(",", ".")
        try:
            number = Decimal(text)
        except Exception:
            return str(value or "")
        return format(number, "f").replace(".", ",")

    @staticmethod
    def _decimal(value: object, fallback: Decimal = Decimal("0")) -> Decimal:
        try:
            return Decimal(str(value or "").replace(",", "."))
        except Exception:
            return fallback

    @staticmethod
    def _money(value: Decimal, currency: str) -> str:
        amount = value.quantize(_CENT, rounding=ROUND_HALF_UP)
        grouped = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        symbol = "\u20ac" if currency == "EUR" else currency
        return f"{grouped} {symbol}"

    @staticmethod
    def _display_date(value: str) -> str:
        parsed = HubFinancePdfService._optional_date(value)
        return parsed.strftime("%d.%m.%Y") if parsed else value

    @staticmethod
    def _optional_date(value: str) -> date | None:
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _required_date(value: str, label: str) -> date:
        parsed = HubFinancePdfService._optional_date(value)
        if parsed is None:
            raise HubFinancePdfError(f"{label} fehlt oder ist ungültig; die ZUGFeRD-PDF wurde nicht erstellt.")
        return parsed

    @staticmethod
    def _filename_stem(identifier: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", identifier.strip()).strip("-.")
        return normalized[:200] or "beleg"

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, (EmailComposeImageError, HubFinancePdfError, HubPdfTemplateError)):
            return str(error)[:2000]
        logger.exception("Finance PDF generation failed")
        return f"PDF-Erzeugung fehlgeschlagen: {type(error).__name__}"[:2000]

    @staticmethod
    def _country_code(value: str) -> str:
        normalized = value.strip().upper()
        if normalized in {"DE", "DEUTSCHLAND", "GERMANY"}:
            return "DE"
        return normalized if len(normalized) == 2 else "DE"


def run_finance_pdf_generation(generation_token: str) -> None:
    """Run one queued PDF job with its own database session."""

    with SessionLocal() as db:
        service = HubFinancePdfService(db=db, cipher=get_secret_cipher())
        try:
            service.generate(generation_token=generation_token)
        except Exception as exc:
            db.rollback()
            service.mark_failed(generation_token=generation_token, error=exc)


def recover_and_process_finance_pdf_generations(*, limit: int = 25) -> int:
    """Resume PDF jobs that were interrupted by an application restart."""

    with SessionLocal() as db:
        interrupted = db.scalars(
            select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.status == "rendering")
        ).all()
        for item in interrupted:
            item.status = "queued" if item.attempt_count < 3 else "failed"
            item.generation_started_at = None
        tokens = tuple(
            db.scalars(
                select(HubFinanceGeneratedPdf.generation_token)
                .where(
                    HubFinanceGeneratedPdf.status == "queued",
                    or_(HubFinanceGeneratedPdf.next_retry_at.is_(None), HubFinanceGeneratedPdf.next_retry_at <= datetime.now(UTC)),
                )
                .order_by(HubFinanceGeneratedPdf.requested_at.asc())
                .limit(limit)
            ).all()
        )
        db.commit()
    for token in tokens:
        run_finance_pdf_generation(token)
    return len(tokens)


def process_queued_finance_pdf_generations(*, limit: int = 25) -> int:
    """Drain queued jobs after startup as well as jobs created by the recurring worker."""

    with SessionLocal() as db:
        tokens = tuple(db.scalars(
            select(HubFinanceGeneratedPdf.generation_token)
            .where(
                HubFinanceGeneratedPdf.status == "queued",
                or_(HubFinanceGeneratedPdf.next_retry_at.is_(None), HubFinanceGeneratedPdf.next_retry_at <= datetime.now(UTC)),
            )
            .order_by(HubFinanceGeneratedPdf.requested_at.asc())
            .limit(limit)
        ).all())
    for token in tokens:
        run_finance_pdf_generation(token)
    return len(tokens)
