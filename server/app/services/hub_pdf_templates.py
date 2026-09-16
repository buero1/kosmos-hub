"""Controlled block editor and versioning for Finance PDF templates."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.hub_legal_terms import HubLegalTerms
from app.models.hub_pdf_template import HubPdfTemplate, HubPdfTemplateRevision
from app.models.hub_user import HubUser
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_legal_terms import HubLegalTermsService
from app.services.template_placeholders import DOCUMENT_NAMES, TemplatePlaceholder as PdfTemplatePlaceholder, pdf_placeholders


class HubPdfTemplateError(ValueError):
    """A safe validation message for the PDF template editor."""


@dataclass(frozen=True)
class PdfTemplateType:
    key: str
    label: str
    singular: str
    default_name: str


@dataclass(frozen=True)
class PdfTemplateBlockDefinition:
    key: str
    label: str
    hint: str


@dataclass(frozen=True)
class PdfTemplateBlockView:
    key: str
    label: str
    hint: str
    content_html: str
    preview_html: str
    is_visible: bool


@dataclass(frozen=True)
class PdfTemplateColumnView:
    key: str
    label: str
    source_key: str
    source_label: str
    sample: str
    width: int
    alignment: str
    is_enabled: bool


@dataclass(frozen=True)
class PdfTemplateEditorView:
    template: HubPdfTemplate
    type_definition: PdfTemplateType
    blocks: tuple[PdfTemplateBlockView, ...]
    columns: tuple[PdfTemplateColumnView, ...]
    placeholders: tuple[PdfTemplatePlaceholder, ...]
    legal_terms: HubLegalTerms | None
    legal_terms_preview_html: str
    editor_data: dict[str, object]


PDF_TEMPLATE_TYPES = (
    PdfTemplateType("offers", "Angebote", "Angebot", "Standardangebot"),
    PdfTemplateType("orders", "Aufträge", "Auftrag", "Standardauftrag"),
    PdfTemplateType("invoices", "Rechnungen", "Rechnung", "Standardrechnung"),
    PdfTemplateType("dunnings", "Mahnungen", "Mahnung", "Standardmahnung"),
)
_TYPE_BY_KEY = {definition.key: definition for definition in PDF_TEMPLATE_TYPES}

PDF_TEMPLATE_BLOCKS = (
    PdfTemplateBlockDefinition("sender", "Absender und Logo", "Briefkopf links oben"),
    PdfTemplateBlockDefinition("title", "Dokumenttitel", "Titel rechts oben"),
    PdfTemplateBlockDefinition("recipient", "Empfänger", "Kunden- und Ansprechpartneradresse"),
    PdfTemplateBlockDefinition("metadata", "Belegdaten", "Nummer, Datum und weitere Kopfdaten"),
    PdfTemplateBlockDefinition("intro", "Einleitung", "Freier Text vor den Positionen"),
    PdfTemplateBlockDefinition("payment", "Zahlungsinformationen", "Hinweise unter den Positionen"),
    PdfTemplateBlockDefinition("footer", "Fußzeile", "Wiederholt sich auf jeder PDF-Seite"),
)
_BLOCK_BY_KEY = {definition.key: definition for definition in PDF_TEMPLATE_BLOCKS}

_LEGACY_PDF_TEMPLATE_PLACEHOLDERS = (
    PdfTemplatePlaceholder("${companyName}", "Firmenname", "Kosmos Medien", "Unternehmen"),
    PdfTemplatePlaceholder("${companyStreet}", "Straße", "Rupert-Mayer-Str. 44", "Unternehmen"),
    PdfTemplatePlaceholder("${companyPostalCode}", "Postleitzahl", "81379", "Unternehmen"),
    PdfTemplatePlaceholder("${companyCity}", "Ort", "München", "Unternehmen"),
    PdfTemplatePlaceholder("${companyPhone}", "Telefon", "+49 (0) 89 740 49 485", "Unternehmen"),
    PdfTemplatePlaceholder("${companyEmail}", "E-Mail", "info@kosmos-medien.de", "Unternehmen"),
    PdfTemplatePlaceholder("${companyIban}", "IBAN", "DE67 7004 0048 0790 0038 00", "Unternehmen"),
    PdfTemplatePlaceholder("${companyBic}", "BIC", "COBADEFFXXX", "Unternehmen"),
    PdfTemplatePlaceholder("${companyTaxId}", "USt-ID", "DE231222930", "Unternehmen"),
    PdfTemplatePlaceholder("${customerName}", "Kundenname", "Schreinerei Muster", "Kunde"),
    PdfTemplatePlaceholder("${contactName}", "Ansprechpartner", "Herr Max Muster", "Kunde"),
    PdfTemplatePlaceholder("${billingStreet}", "Rechnungsstraße", "Musterstraße 12", "Kunde"),
    PdfTemplatePlaceholder("${billingPostalCode}", "Rechnungs-PLZ", "80331", "Kunde"),
    PdfTemplatePlaceholder("${billingCity}", "Rechnungsort", "München", "Kunde"),
    PdfTemplatePlaceholder("${documentTitle}", "Dokumenttitel", "Auftrag Firmenwebsite", "Beleg"),
    PdfTemplatePlaceholder("${documentNumber}", "Belegnummer", "SO-00154", "Beleg"),
    PdfTemplatePlaceholder("${documentDate}", "Belegdatum", "02.07.2026", "Beleg"),
    PdfTemplatePlaceholder("${validUntil}", "Gültig bis", "16.07.2026", "Beleg"),
    PdfTemplatePlaceholder("${dueDate}", "Fällig am", "16.07.2026", "Beleg"),
    PdfTemplatePlaceholder("${paymentTerms}", "Zahlungsbedingungen", "14 Tage", "Beleg"),
    PdfTemplatePlaceholder("${netTotal}", "Nettosumme", "448,99 €", "Summen"),
    PdfTemplatePlaceholder("${taxTotal}", "USt.-Summe", "85,31 €", "Summen"),
    PdfTemplatePlaceholder("${grossTotal}", "Gesamtsumme", "534,30 €", "Summen"),
)
_PLACEHOLDER_SAMPLE = {
    placeholder.token: placeholder.sample
    for placeholder in (*_LEGACY_PDF_TEMPLATE_PLACEHOLDERS, *(item for kind in ("offers", "orders", "invoices", "dunnings") for item in pdf_placeholders(kind)))
}

PDF_LINE_SOURCES = (
    ("position", "Positionsnummer", "1"),
    ("article_name", "Artikelname", 'Monatsbeitrag Homepage "Basic"'),
    ("article_description", "Artikelbeschreibung", "Website-Paket und laufende Betreuung"),
    ("sku", "Artikelnummer", "ART-0001"),
    ("quantity", "Menge", "1,00"),
    ("unit", "Einheit", "Monatlich"),
    ("unit_price", "Tarif", "49,99 €"),
    ("tax_rate", "USt.", "19 %"),
    ("line_total", "Betrag", "49,99 €"),
)
_LINE_SOURCE_BY_KEY = {key: (label, sample) for key, label, sample in PDF_LINE_SOURCES}

_DEFAULT_CONTENT_BY_TYPE = {
    "offers": {
        "title": "<h1>Angebot ${Offer.Number}</h1>",
        "intro": "<p>${Contact.Greeting},</p><p>vielen Dank für Ihr Interesse. Gerne bieten wir Ihnen die folgenden Leistungen an.</p>",
        "payment": "<p><strong>Gültig bis:</strong> ${Offer.ValidUntil}<br><strong>Zahlungsbedingungen:</strong> ${Offer.PaymentTerms}</p>",
    },
    "orders": {
        "title": "<h1>Auftrag ${Order.Number}</h1>",
        "intro": "<p>${Contact.Greeting},</p><p>unten finden Sie den vereinbarten Leistungsumfang Ihres Auftrags.</p>",
        "payment": "<p><strong>Laufzeit und Zahlungsmodalitäten:</strong><br>${Order.PaymentTerms}</p>",
    },
    "invoices": {
        "title": "<h1>Rechnung ${Invoice.Number}</h1>",
        "intro": "<p>${Contact.Greeting},</p><p>für die folgenden Leistungen erlauben wir uns, Ihnen den ausgewiesenen Betrag zu berechnen.</p>",
        "payment": "<p><strong>Fällig am:</strong> ${Invoice.DueDate}<br><strong>Zahlungsbedingungen:</strong> ${Invoice.PaymentTerms}<br><strong>Gesamt:</strong> ${Invoice.GrossTotal}</p>",
    },
    "dunnings": {
        "title": "<h1>Mahnung ${Dunning.Number}</h1>",
        "intro": "<p>${Contact.Greeting},</p><p>zu unserer Rechnung ${Dunning.SourceInvoiceNumber} konnten wir bislang keinen vollständigen Zahlungseingang feststellen.</p>",
        "payment": "<p><strong>Neue Zahlungsfrist:</strong> ${Dunning.DueDate}<br><strong>Offener Gesamtbetrag:</strong> ${Dunning.GrossTotal}</p>",
    },
}


class HubPdfTemplateService:
    """Manage safe, versioned PDF template structures."""

    def __init__(self, *, db: Session):
        self.db = db

    def ensure_default_templates(self) -> None:
        default_legal_terms = HubLegalTermsService(db=self.db).ensure_default()
        existing_types = set(self.db.scalars(select(HubPdfTemplate.document_type)).all())
        for definition in PDF_TEMPLATE_TYPES:
            if definition.key in existing_types:
                continue
            template = HubPdfTemplate(
                document_type=definition.key,
                name=definition.default_name,
                is_default=True,
                version=1,
                content_json=self._encode(self._default_content(definition.key)),
                legal_terms_id=default_legal_terms.id if definition.key in {"offers", "orders"} else None,
                created_by_username="system",
            )
            self.db.add(template)
            self.db.flush()
            self._add_revision(template=template, actor_username="system")
        unassigned = self.db.scalars(
            select(HubPdfTemplate).where(
                HubPdfTemplate.document_type.in_(("offers", "orders")),
                HubPdfTemplate.legal_terms_id.is_(None),
            )
        ).all()
        for template in unassigned:
            template.legal_terms_id = default_legal_terms.id
            self._version(template=template, actor_username="system")
        self.db.flush()

    def list_templates(self, *, document_type: str | None = None) -> tuple[HubPdfTemplate, ...]:
        self.ensure_default_templates()
        statement = select(HubPdfTemplate).order_by(
            HubPdfTemplate.document_type.asc(),
            HubPdfTemplate.is_default.desc(),
            HubPdfTemplate.name.asc(),
            HubPdfTemplate.id.asc(),
        )
        if document_type is not None:
            self._type(document_type)
            statement = statement.where(HubPdfTemplate.document_type == document_type)
        return tuple(self.db.scalars(statement).all())

    def get(self, template_id: int) -> HubPdfTemplate | None:
        return self.db.get(HubPdfTemplate, template_id)

    def revision_for(self, template: HubPdfTemplate) -> HubPdfTemplateRevision:
        revision = self.db.scalar(
            select(HubPdfTemplateRevision).where(
                HubPdfTemplateRevision.template_id == template.id,
                HubPdfTemplateRevision.version == template.version,
            )
        )
        if revision is None:
            raise HubPdfTemplateError("Die aktuelle Vorlagenversion wurde nicht gefunden.")
        return revision

    def decoded_content(self, *, document_type: str, content_json: str) -> dict[str, object]:
        return self._decode(content_json, document_type=document_type)

    def default_for(self, document_type: str) -> HubPdfTemplate:
        self.ensure_default_templates()
        definition = self._type(document_type)
        template = self.db.scalar(
            select(HubPdfTemplate).where(
                HubPdfTemplate.document_type == definition.key,
                HubPdfTemplate.is_default.is_(True),
            )
        )
        if template is None:
            raise HubPdfTemplateError("Für diese Belegart ist keine Standardvorlage vorhanden.")
        return template

    def editor_view(self, template: HubPdfTemplate) -> PdfTemplateEditorView:
        definition = self._type(template.document_type)
        content = self._decode(template.content_json, document_type=definition.key)
        blocks_data = content["blocks"]
        blocks = tuple(
            PdfTemplateBlockView(
                key=block.key,
                label=block.label,
                hint=block.hint,
                content_html=str(blocks_data[block.key]["content_html"]),
                preview_html=self._preview_html(str(blocks_data[block.key]["content_html"])),
                is_visible=bool(blocks_data[block.key]["is_visible"]),
            )
            for block in PDF_TEMPLATE_BLOCKS
        )
        columns = tuple(self._column_view(column) for column in content["positions"]["columns"])
        legal_terms = template.legal_terms if definition.key in {"offers", "orders"} else None
        return PdfTemplateEditorView(
            template=template,
            type_definition=definition,
            blocks=blocks,
            columns=columns,
            placeholders=pdf_placeholders(definition.key),
            legal_terms=legal_terms,
            legal_terms_preview_html=legal_terms.content_html if legal_terms is not None else "",
            editor_data={
                "templateId": template.id,
                "blocks": {
                    block.key: {
                        "label": block.label,
                        "hint": block.hint,
                        "contentHtml": block.content_html,
                        "isVisible": block.is_visible,
                    }
                    for block in blocks
                },
            },
        )

    def create(self, *, actor: HubUser, document_type: str, name: str) -> HubPdfTemplate:
        self._require_admin(actor)
        definition = self._type(document_type)
        template = HubPdfTemplate(
            document_type=definition.key,
            name=self._name(name),
            is_default=False,
            version=1,
            content_json=self._encode(self._default_content(definition.key)),
            legal_terms_id=(
                HubLegalTermsService(db=self.db).ensure_default().id
                if definition.key in {"offers", "orders"}
                else None
            ),
            created_by_username=actor.username,
        )
        self.db.add(template)
        self.db.flush()
        self._add_revision(template=template, actor_username=actor.username)
        return template

    def rename(self, *, actor: HubUser, template_id: int, name: str) -> HubPdfTemplate:
        template = self._required(actor=actor, template_id=template_id)
        normalized_name = self._name(name)
        if normalized_name == template.name:
            return template
        template.name = normalized_name
        self._version(template=template, actor_username=actor.username)
        return template

    def set_legal_terms(
        self,
        *,
        actor: HubUser,
        template_id: int,
        legal_terms_id: int | None,
    ) -> HubPdfTemplate:
        template = self._required(actor=actor, template_id=template_id)
        if template.document_type not in {"offers", "orders"}:
            raise HubPdfTemplateError("Diese Belegvorlage enthält keinen AGB-Bereich.")
        legal_terms = None
        if legal_terms_id is not None:
            legal_terms = HubLegalTermsService(db=self.db).get(legal_terms_id)
            if legal_terms is None:
                raise HubPdfTemplateError("Die ausgewählte AGB wurde nicht gefunden.")
        if template.legal_terms_id == (legal_terms.id if legal_terms is not None else None):
            return template
        template.legal_terms_id = legal_terms.id if legal_terms is not None else None
        self._version(template=template, actor_username=actor.username)
        return template

    def update_block(
        self,
        *,
        actor: HubUser,
        template_id: int,
        block_key: str,
        content_html: str,
        is_visible: bool,
    ) -> HubPdfTemplate:
        template = self._required(actor=actor, template_id=template_id)
        if block_key not in _BLOCK_BY_KEY:
            raise HubPdfTemplateError("Der Vorlagenbereich wurde nicht gefunden.")
        normalized_html = self._sanitize_html(content_html, allow_empty=not is_visible)
        content = self._decode(template.content_json, document_type=template.document_type)
        content["blocks"][block_key] = {
            "content_html": normalized_html,
            "is_visible": is_visible,
        }
        template.content_json = self._encode(content)
        self._version(template=template, actor_username=actor.username)
        return template

    def update_positions(
        self,
        *,
        actor: HubUser,
        template_id: int,
        columns: list[dict[str, object]],
    ) -> HubPdfTemplate:
        template = self._required(actor=actor, template_id=template_id)
        validated = self._validated_columns(columns)
        content = self._decode(template.content_json, document_type=template.document_type)
        content["positions"] = {"columns": validated}
        template.content_json = self._encode(content)
        self._version(template=template, actor_username=actor.username)
        return template

    def duplicate(self, *, actor: HubUser, template_id: int) -> HubPdfTemplate:
        source = self._required(actor=actor, template_id=template_id)
        duplicate = HubPdfTemplate(
            document_type=source.document_type,
            name=self._copy_name(source.name),
            is_default=False,
            version=1,
            content_json=source.content_json,
            legal_terms_id=source.legal_terms_id,
            created_by_username=actor.username,
        )
        self.db.add(duplicate)
        self.db.flush()
        self._add_revision(template=duplicate, actor_username=actor.username)
        return duplicate

    def set_default(self, *, actor: HubUser, template_id: int) -> HubPdfTemplate:
        template = self._required(actor=actor, template_id=template_id)
        for candidate in self.list_templates(document_type=template.document_type):
            candidate.is_default = candidate.id == template.id
        self.db.flush()
        return template

    def delete(self, *, actor: HubUser, template_id: int) -> str:
        template = self._required(actor=actor, template_id=template_id)
        alternatives = [
            candidate
            for candidate in self.list_templates(document_type=template.document_type)
            if candidate.id != template.id
        ]
        if not alternatives:
            raise HubPdfTemplateError("Die letzte Vorlage einer Belegart kann nicht gelöscht werden.")
        if template.is_default:
            alternatives[0].is_default = True
        document_type = template.document_type
        self.db.delete(template)
        self.db.flush()
        return document_type

    def _required(self, *, actor: HubUser, template_id: int) -> HubPdfTemplate:
        self._require_admin(actor)
        template = self.get(template_id)
        if template is None:
            raise HubPdfTemplateError("Die PDF-Vorlage wurde nicht gefunden.")
        return template

    def _version(self, *, template: HubPdfTemplate, actor_username: str) -> None:
        template.version += 1
        self.db.flush()
        self._add_revision(template=template, actor_username=actor_username)

    def _add_revision(self, *, template: HubPdfTemplate, actor_username: str) -> None:
        legal_terms_revision_id = None
        if template.legal_terms_id is not None:
            legal_terms = HubLegalTermsService(db=self.db).get(template.legal_terms_id)
            if legal_terms is None:
                raise HubPdfTemplateError("Die ausgewählte AGB wurde nicht gefunden.")
            legal_terms_revision_id = HubLegalTermsService(db=self.db).revision_for(legal_terms).id
        self.db.add(
            HubPdfTemplateRevision(
                template_id=template.id,
                version=template.version,
                name=template.name,
                content_json=template.content_json,
                legal_terms_revision_id=legal_terms_revision_id,
                created_by_username=actor_username,
            )
        )
        self.db.flush()

    def _copy_name(self, source_name: str) -> str:
        existing = {
            template.name.casefold()
            for template in self.list_templates()
        }
        candidate = f"{source_name} (Kopie)"[:255]
        index = 2
        while candidate.casefold() in existing:
            suffix = f" (Kopie {index})"
            candidate = f"{source_name[:255 - len(suffix)]}{suffix}"
            index += 1
        return candidate

    @staticmethod
    def _require_admin(actor: HubUser) -> None:
        if actor.role != "admin":
            raise HubPdfTemplateError("Nur Hub-Administratoren können PDF-Vorlagen ändern.")

    @staticmethod
    def _name(value: str) -> str:
        normalized = " ".join(value.split())
        if not 3 <= len(normalized) <= 255:
            raise HubPdfTemplateError("Der Vorlagenname muss zwischen 3 und 255 Zeichen lang sein.")
        return normalized

    @staticmethod
    def _type(document_type: str) -> PdfTemplateType:
        definition = _TYPE_BY_KEY.get(document_type)
        if definition is None:
            raise HubPdfTemplateError("Die Belegart wurde nicht gefunden.")
        return definition

    @staticmethod
    def _encode(content: dict[str, object]) -> str:
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _decode(cls, raw: str, *, document_type: str) -> dict[str, object]:
        fallback = cls._default_content(document_type)
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return fallback
        if not isinstance(decoded, dict):
            return fallback
        blocks = decoded.get("blocks")
        positions = decoded.get("positions")
        if not isinstance(blocks, dict) or not isinstance(positions, dict):
            return fallback
        for definition in PDF_TEMPLATE_BLOCKS:
            value = blocks.get(definition.key)
            if not isinstance(value, dict) or not isinstance(value.get("content_html"), str):
                blocks[definition.key] = fallback["blocks"][definition.key]
            else:
                value["is_visible"] = bool(value.get("is_visible", True))
        try:
            positions["columns"] = cls._validated_columns(positions.get("columns", []))
        except HubPdfTemplateError:
            positions = fallback["positions"]
        return {"blocks": blocks, "positions": positions}

    @classmethod
    def _default_content(cls, document_type: str) -> dict[str, object]:
        definition = cls._type(document_type)
        specialized = _DEFAULT_CONTENT_BY_TYPE[definition.key]
        defaults = {
            "sender": "<p><strong>${Company.Name}</strong><br>${Company.Street}<br>${Company.PostalCode} ${Company.City}</p>",
            "title": specialized["title"],
            "recipient": "<p><strong>${Customer.Name}</strong><br>${Contact.Name}<br>${Customer.BillingStreet}<br>${Customer.BillingPostalCode} ${Customer.BillingCity}</p>",
            "metadata": f"<p><strong>Datum:</strong> ${{{DOCUMENT_NAMES[definition.key][0]}.Date}}<br><strong>Nummer:</strong> ${{{DOCUMENT_NAMES[definition.key][0]}.Number}}</p>",
            "intro": specialized["intro"],
            "payment": specialized["payment"],
            "footer": (
                "<table style=\"width:100%;border-collapse:collapse\"><tbody><tr>"
                "<td style=\"width:33%;vertical-align:top\">${Company.Name}<br>${Company.Street}<br>${Company.PostalCode} ${Company.City}</td>"
                "<td style=\"width:34%;vertical-align:top;text-align:center\">Tel: ${Company.Phone}<br>${Company.Email}</td>"
                "<td style=\"width:33%;vertical-align:top;text-align:right\">IBAN: ${Company.Iban}<br>BIC: ${Company.Bic}<br>USt-ID: ${Company.TaxId}</td>"
                "</tr></tbody></table>"
            ),
        }
        columns = cls._default_columns(document_type)
        return {
            "blocks": {
                key: {"content_html": cls._sanitize_html(value), "is_visible": True}
                for key, value in defaults.items()
            },
            "positions": {"columns": columns},
        }

    @staticmethod
    def _default_columns(document_type: str) -> list[dict[str, object]]:
        invoice = document_type in {"invoices", "dunnings"}
        widths = {
            "position": 6,
            "article_name": 34 if invoice else 42,
            "article_description": 30,
            "sku": 12,
            "quantity": 10,
            "unit": 10 if invoice else 12,
            "unit_price": 13 if invoice else 14,
            "tax_rate": 11,
            "line_total": 16,
        }
        labels = {
            "position": "#",
            "article_name": "Artikel & Beschreibung",
            "article_description": "Beschreibung",
            "sku": "Artikelnummer",
            "quantity": "Menge",
            "unit": "Einheit",
            "unit_price": "Tarif",
            "tax_rate": "USt.",
            "line_total": "Betrag",
        }
        return [
            {
                "key": key,
                "label": labels[key],
                "source_key": key,
                "width": width,
                "alignment": "left" if key in {"article_name", "unit"} else "right",
                "is_enabled": key not in {"article_description", "sku"} and (key != "tax_rate" or invoice),
            }
            for key, width in widths.items()
        ]

    @classmethod
    def _validated_columns(cls, columns: object) -> list[dict[str, object]]:
        if not isinstance(columns, list):
            raise HubPdfTemplateError("Die Tabellenkonfiguration ist ungültig.")
        available_keys = set(_LINE_SOURCE_BY_KEY)
        seen: set[str] = set()
        validated: list[dict[str, object]] = []
        for raw in columns:
            if not isinstance(raw, dict):
                raise HubPdfTemplateError("Die Tabellenkonfiguration ist ungültig.")
            key = str(raw.get("key", "")).strip()
            source_key = str(raw.get("source_key", "")).strip()
            if key not in available_keys or key in seen or source_key not in available_keys:
                raise HubPdfTemplateError("Die Tabellenspalten haben sich geändert. Bitte lade die Seite neu.")
            seen.add(key)
            label = " ".join(str(raw.get("label", "")).split())
            if not 1 <= len(label) <= 80:
                raise HubPdfTemplateError("Jede sichtbare Spalte benötigt eine kurze Überschrift.")
            try:
                width = int(raw.get("width", 0))
            except (TypeError, ValueError) as exc:
                raise HubPdfTemplateError("Die Spaltenbreite muss eine ganze Zahl sein.") from exc
            if not 4 <= width <= 80:
                raise HubPdfTemplateError("Eine Spaltenbreite muss zwischen 4 und 80 Prozent liegen.")
            alignment = str(raw.get("alignment", "left"))
            if alignment not in {"left", "center", "right"}:
                raise HubPdfTemplateError("Die Spaltenausrichtung ist ungültig.")
            validated.append({
                "key": key,
                "label": label,
                "source_key": source_key,
                "width": width,
                "alignment": alignment,
                "is_enabled": bool(raw.get("is_enabled", False)),
            })
        if seen != available_keys:
            raise HubPdfTemplateError("Die Tabellenspalten haben sich geändert. Bitte lade die Seite neu.")
        enabled = [column for column in validated if column["is_enabled"]]
        if not 2 <= len(enabled) <= len(available_keys):
            raise HubPdfTemplateError("Die Positionstabelle benötigt mindestens zwei sichtbare Spalten.")
        if sum(int(column["width"]) for column in enabled) != 100:
            raise HubPdfTemplateError("Die Breiten der sichtbaren Spalten müssen zusammen 100 Prozent ergeben.")
        return validated

    @staticmethod
    def _sanitize_html(value: str, *, allow_empty: bool = False) -> str:
        normalized = value.strip()
        if not normalized and allow_empty:
            return ""
        try:
            return CustomerCommunicationService._sanitized_email_content(
                normalized,
                allow_template_href_placeholders=True,
            )
        except ValueError as exc:
            raise HubPdfTemplateError(str(exc).replace("Nachricht", "Vorlagenbereich")) from exc

    @staticmethod
    def _preview_html(content_html: str) -> str:
        preview = content_html
        for token, sample in _PLACEHOLDER_SAMPLE.items():
            preview = preview.replace(token, escape(sample))
        return re.sub(
            r"\$\{[A-Za-z][A-Za-z0-9.]*\}",
            lambda match: f'<span class="pdf-template-unresolved-placeholder">{escape(match.group(0))}</span>',
            preview,
        )

    @staticmethod
    def _column_view(column: dict[str, object]) -> PdfTemplateColumnView:
        source_key = str(column["source_key"])
        source_label, sample = _LINE_SOURCE_BY_KEY[source_key]
        return PdfTemplateColumnView(
            key=str(column["key"]),
            label=str(column["label"]),
            source_key=source_key,
            source_label=source_label,
            sample=sample,
            width=int(column["width"]),
            alignment=str(column["alignment"]),
            is_enabled=bool(column["is_enabled"]),
        )
