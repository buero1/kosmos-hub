"""Canonical, user-facing placeholders shared by email and finance PDFs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from app.core.config import get_settings

from app.services.hub_case_field_catalog import HUB_CASE_FIELDS
from app.services.hub_finance_document_field_catalog import (
    DUNNING_FIELDS,
    INVOICE_FIELDS,
    ORDER_FIELDS,
    RECURRING_INVOICE_FIELDS,
)
from app.services.hub_finance_field_catalog import OFFER_FIELDS
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS
from app.services.zoho_contact_field_catalog import ZOHO_CONTACT_FIELDS


@dataclass(frozen=True)
class TemplatePlaceholder:
    token: str
    label: str
    sample: str
    group: str
    profile_key: str = ""
    hint: str = ""
    contexts: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailTemplateContext:
    key: str
    label: str


EMAIL_TEMPLATE_CONTEXTS = (
    EmailTemplateContext("general", "Allgemein"),
    EmailTemplateContext("customers", "Kunden & Kontakte"),
    EmailTemplateContext("leads", "Leads"),
    EmailTemplateContext("cases", "Fälle"),
    EmailTemplateContext("offers", "Angebote"),
    EmailTemplateContext("orders", "Aufträge"),
    EmailTemplateContext("invoices", "Rechnungen"),
    EmailTemplateContext("dunnings", "Mahnungen"),
    EmailTemplateContext("recurring-invoices", "Periodische Rechnungen"),
    EmailTemplateContext("tasks", "Aufgaben"),
    EmailTemplateContext("calls", "Anrufe"),
    EmailTemplateContext("meetings", "Meetings"),
    EmailTemplateContext("sites", "Sites"),
)
EMAIL_TEMPLATE_CONTEXT_KEYS = frozenset(item.key for item in EMAIL_TEMPLATE_CONTEXTS)


COMPANY_PLACEHOLDERS = (
    TemplatePlaceholder("${Company.Name}", "Firmenname", "Kosmos Medien", "Firma & globale Werte"),
    TemplatePlaceholder("${Company.Street}", "Straße", "Rupert-Mayer-Str. 44", "Firma & globale Werte"),
    TemplatePlaceholder("${Company.PostalCode}", "Postleitzahl", "81379", "Firma & globale Werte"),
    TemplatePlaceholder("${Company.City}", "Ort", "München", "Firma & globale Werte"),
    TemplatePlaceholder("${Company.Phone}", "Telefon", "+49 (0) 89 740 49 485", "Firma & globale Werte"),
    TemplatePlaceholder("${Company.Email}", "E-Mail", "info@kosmos-medien.de", "Firma & globale Werte"),
    TemplatePlaceholder("${Company.Iban}", "IBAN", "DE67 7004 0048 0790 0038 00", "Firma & globale Werte"),
    TemplatePlaceholder("${Company.Bic}", "BIC", "COBADEFFXXX", "Firma & globale Werte"),
    TemplatePlaceholder("${Company.TaxId}", "USt-ID", "DE231222930", "Firma & globale Werte"),
)
EMAIL_SIGNATURE_PLACEHOLDER = TemplatePlaceholder(
    "${Company.EmailSignature}", "E-Mail-Signatur", "Mit freundlichen Grüßen", "Firma & globale Werte", hint="Nur im E-Mail-Inhalt",
)

USER_PLACEHOLDERS = (
    TemplatePlaceholder("${User.Name}", "Vollständiger Name", "Max Muster", "Schreibender Benutzer"),
    TemplatePlaceholder("${User.FirstName}", "Vorname", "Max", "Schreibender Benutzer"),
    TemplatePlaceholder("${User.LastName}", "Nachname", "Muster", "Schreibender Benutzer"),
)

CUSTOMER_PLACEHOLDERS = (
    TemplatePlaceholder("${Customer.Name}", "Kundenname", "Schreinerei Muster", "Kunde"),
    TemplatePlaceholder("${Customer.CustomerNumber}", "Kundennummer", "10042", "Kunde", "customer_number"),
    TemplatePlaceholder("${Customer.BillingStreet}", "Rechnungsstraße", "Musterstraße 12", "Kunde", "billing_street"),
    TemplatePlaceholder("${Customer.BillingPostalCode}", "Rechnungs-PLZ", "80331", "Kunde", "billing_postal_code"),
    TemplatePlaceholder("${Customer.BillingCity}", "Rechnungsort", "München", "Kunde", "billing_city"),
    TemplatePlaceholder("${Customer.Phone}", "Telefon", "089 123456", "Kunde", "phone"),
    TemplatePlaceholder("${Customer.Website}", "Website", "https://beispiel.de", "Kunde", "website"),
)

INVOICE_CUSTOMER_BANK_PLACEHOLDERS = (
    TemplatePlaceholder("${Customer.Iban}", "IBAN", "DE89 3704 0044 0532 0130 00", "Kunde", "iban"),
    TemplatePlaceholder("${Customer.Bic}", "BIC", "COBADEFFXXX", "Kunde", "bic"),
    TemplatePlaceholder("${Customer.Bank}", "Bankname", "Beispielbank", "Kunde", "bank"),
)

CONTACT_PLACEHOLDERS = (
    TemplatePlaceholder("${Contact.Name}", "Name", "Max Muster", "Verknüpfter Kontakt"),
    TemplatePlaceholder("${Contact.Greeting}", "Briefanrede", "Sehr geehrter Herr Muster", "Verknüpfter Kontakt"),
    TemplatePlaceholder("${Contact.Salutation}", "Anrede", "Herr", "Verknüpfter Kontakt", "salutation"),
    TemplatePlaceholder("${Contact.FirstName}", "Vorname", "Max", "Verknüpfter Kontakt", "first_name"),
    TemplatePlaceholder("${Contact.LastName}", "Nachname", "Muster", "Verknüpfter Kontakt", "last_name"),
    TemplatePlaceholder("${Contact.Email}", "E-Mail", "max@beispiel.de", "Verknüpfter Kontakt", "email"),
    TemplatePlaceholder("${Contact.Title}", "Titel", "Dr.", "Verknüpfter Kontakt", "title"),
    TemplatePlaceholder("${Contact.Phone}", "Telefon", "089 123456", "Verknüpfter Kontakt", "phone"),
    TemplatePlaceholder("${Contact.MailingStreet}", "Poststraße", "Musterstraße 12", "Verknüpfter Kontakt", "mailing_street"),
    TemplatePlaceholder("${Contact.MailingPostalCode}", "Post-PLZ", "80331", "Verknüpfter Kontakt", "mailing_postal_code"),
    TemplatePlaceholder("${Contact.MailingCity}", "Postort", "München", "Verknüpfter Kontakt", "mailing_city"),
)

DOCUMENT_NAMES = {
    "offers": ("Offer", "Aktuelles Angebot"),
    "orders": ("Order", "Aktueller Auftrag"),
    "invoices": ("Invoice", "Aktuelle Rechnung"),
    "dunnings": ("Dunning", "Aktuelle Mahnung"),
}
DOCUMENT_FIELDS = (
    ("Number", "Nummer", "RE-004035"),
    ("Title", "Titel", "Rechnung"),
    ("Status", "Status", "Entwurf"),
    ("Date", "Datum", "12.09.2026"),
    ("DueDate", "Fällig am", "26.09.2026"),
    ("SourceInvoiceNumber", "Rechnungsnummer", "RE-004035"),
    ("ValidUntil", "Gültig bis", "26.09.2026"),
    ("PaymentTerms", "Zahlungsziel", "14 Tage"),
    ("NetTotal", "Nettosumme", "448,99 €"),
    ("TaxTotal", "USt.-Summe", "85,31 €"),
    ("GrossTotal", "Gesamtsumme", "534,30 €"),
)
_DOCUMENT_VISIBLE_FIELDS = {
    "offers": {"Number", "Title", "Status", "Date", "ValidUntil", "PaymentTerms", "NetTotal", "TaxTotal", "GrossTotal"},
    "orders": {"Number", "Title", "Status", "Date", "PaymentTerms", "NetTotal", "TaxTotal", "GrossTotal"},
    "invoices": {"Number", "Title", "Status", "Date", "DueDate", "PaymentTerms", "NetTotal", "TaxTotal", "GrossTotal"},
    "dunnings": {"Number", "Title", "Status", "Date", "DueDate", "SourceInvoiceNumber", "NetTotal", "TaxTotal", "GrossTotal"},
}
_DOCUMENT_EMAIL_CONTEXTS = {
    "offers": ("offers", "orders"),
    "orders": ("orders", "invoices"),
    "invoices": ("invoices", "dunnings"),
    "dunnings": ("dunnings",),
}

_CUSTOMER_EMAIL_CONTEXTS = (
    "general", "customers", "cases", "offers", "orders", "invoices", "dunnings",
    "recurring-invoices", "tasks", "calls", "meetings", "sites",
)
_CONTACT_EMAIL_CONTEXTS = (
    "general", "customers", "cases", "offers", "orders", "invoices", "dunnings",
    "recurring-invoices", "sites",
)
_LEAD_EMAIL_CONTEXTS = ("leads", "tasks", "calls", "meetings")


def _pascal_case(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in value.split("_") if part)


def _catalog_sample(display_type: str) -> str:
    return {
        "Autonummer": "RE-004035",
        "Boolesch": "Ja",
        "Datum": "16.09.2026",
        "DatumZeit": "16.09.2026 10:30",
        "Datum und Uhrzeit": "16.09.2026 10:30",
        "Dezimalzahl": "12",
        "E-Mail": "max@beispiel.de",
        "Ganzzahl": "1",
        "Telefon": "+49 89 123456",
        "URL": "https://beispiel.de",
        "Waehrung": "100,00 €",
        "Zahlungsziel": "14 Tage",
    }.get(display_type, "Beispiel")


def _catalog_placeholders(
    namespace: str,
    group: str,
    fields: tuple[object, ...],
    contexts: tuple[str, ...],
    *,
    profile_keys: bool = False,
    token_fields: Mapping[str, str] | None = None,
) -> tuple[TemplatePlaceholder, ...]:
    token_fields = token_fields or {}
    return tuple(
        TemplatePlaceholder(
            f"${{{namespace}.{token_fields.get(field.key, _pascal_case(field.key))}}}",
            field.label,
            _catalog_sample(field.display_type),
            group,
            profile_key=field.key if profile_keys else "",
            contexts=contexts,
        )
        for field in fields
    )


def _prefer_placeholder_definitions(
    preferred: tuple[TemplatePlaceholder, ...],
    generated: tuple[TemplatePlaceholder, ...],
) -> tuple[TemplatePlaceholder, ...]:
    """Keep established labels/samples while adding every catalog-backed field."""
    generated_by_token = {item.token: item for item in generated}
    merged = [
        replace(
            item,
            profile_key=generated_by_token[item.token].profile_key,
            contexts=generated_by_token[item.token].contexts,
        ) if item.token in generated_by_token else item
        for item in preferred
    ]
    seen = {item.token for item in merged}
    merged.extend(item for item in generated if item.token not in seen)
    return tuple(merged)


_CUSTOMER_FIELD_TOKENS = {
    "customer_name": "Name",
    "customer_number": "CustomerNumber",
    "billing_street": "BillingStreet",
    "billing_postal_code": "BillingPostalCode",
    "billing_city": "BillingCity",
    "phone": "Phone",
    "website": "Website",
}
_CONTACT_FIELD_TOKENS = {
    "salutation": "Salutation",
    "first_name": "FirstName",
    "last_name": "LastName",
    "title": "Title",
    "email": "Email",
    "phone": "Phone",
    "mailing_street": "MailingStreet",
    "mailing_postal_code": "MailingPostalCode",
    "mailing_city": "MailingCity",
}

EMAIL_CUSTOMER_PLACEHOLDERS = _prefer_placeholder_definitions(
    tuple(replace(item, contexts=_CUSTOMER_EMAIL_CONTEXTS) for item in CUSTOMER_PLACEHOLDERS),
    _catalog_placeholders(
        "Customer",
        "Kunde",
        tuple(field for field in ZOHO_ACCOUNT_FIELDS if not field.sensitive and not field.subform_parent),
        _CUSTOMER_EMAIL_CONTEXTS,
        profile_keys=True,
        token_fields=_CUSTOMER_FIELD_TOKENS,
    ),
)
EMAIL_CONTACT_PLACEHOLDERS = _prefer_placeholder_definitions(
    tuple(replace(item, contexts=_CONTACT_EMAIL_CONTEXTS) for item in CONTACT_PLACEHOLDERS),
    _catalog_placeholders(
        "Contact",
        "Verknüpfter Kontakt",
        ZOHO_CONTACT_FIELDS,
        _CONTACT_EMAIL_CONTEXTS,
        profile_keys=True,
        token_fields=_CONTACT_FIELD_TOKENS,
    ),
)


LEAD_PLACEHOLDERS = (
    TemplatePlaceholder("${Lead.Greeting}", "Briefanrede", "Sehr geehrter Herr Muster", "Aktueller Lead", contexts=_LEAD_EMAIL_CONTEXTS),
    *_catalog_placeholders("Lead", "Aktueller Lead", HUB_LEAD_FIELDS, _LEAD_EMAIL_CONTEXTS, profile_keys=True),
)
CASE_PLACEHOLDERS = _catalog_placeholders("Case", "Aktueller Fall", HUB_CASE_FIELDS, ("cases", "tasks"), profile_keys=True)
RECURRING_INVOICE_PLACEHOLDERS = (
    TemplatePlaceholder("${RecurringInvoice.Name}", "Bezeichnung", "Homepage-Betreuung", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.Status}", "Status", "Aktiv", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.StartDate}", "Startdatum", "16.09.2026", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.EndDate}", "Enddatum", "16.09.2027", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.Interval}", "Rhythmus", "Monatlich", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.NextInvoiceDate}", "Nächstes Rechnungsdatum", "01.10.2026", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.PaymentDue}", "Zahlungsziel", "14 Tage", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
)
RECURRING_INVOICE_PLACEHOLDERS = _prefer_placeholder_definitions(
    RECURRING_INVOICE_PLACEHOLDERS,
    _catalog_placeholders(
        "RecurringInvoice",
        "Aktuelle periodische Rechnung",
        RECURRING_INVOICE_FIELDS,
        ("recurring-invoices",),
        profile_keys=True,
        token_fields={
            "interval_unit": "Interval",
            "next_invoice_date": "NextInvoiceDate",
            "payment_due": "PaymentDue",
        },
    ),
)
TASK_PLACEHOLDERS = (
    TemplatePlaceholder("${Task.Title}", "Bezeichnung", "Unterlagen prüfen", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.Status}", "Status", "Geplant", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.DueAt}", "Fällig am", "16.09.2026 10:30", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.ReminderChannel}", "Erinnerungsart", "E-Mail", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.ReminderMinutesBefore}", "Erinnerung vorher in Minuten", "30", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.Description}", "Beschreibung", "Bitte Unterlagen prüfen.", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.CreatedBy}", "Erstellt von", "Team Kosmos", "Aktuelle Aufgabe", contexts=("tasks",)),
)
CALL_PLACEHOLDERS = (
    TemplatePlaceholder("${Call.Title}", "Bezeichnung", "Rückruf", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.Status}", "Status", "Geplant", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.Direction}", "Richtung", "Ausgehend", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.StartsAt}", "Beginn", "16.09.2026 10:30", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.EndsAt}", "Ende", "16.09.2026 11:00", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.DurationMinutes}", "Dauer in Minuten", "30", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.ReminderChannel}", "Erinnerungsart", "Lokal", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.ReminderMinutesBefore}", "Erinnerung vorher in Minuten", "15", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.Description}", "Beschreibung", "Telefonische Abstimmung.", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.CreatedBy}", "Erstellt von", "Team Kosmos", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.RecordingUrl}", "Aufzeichnungs-URL", "https://beispiel.de/anruf", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.TranscriptUrl}", "Transkript-URL", "https://beispiel.de/transkript", "Aktueller Anruf", contexts=("calls",)),
)
MEETING_PLACEHOLDERS = (
    TemplatePlaceholder("${Meeting.Title}", "Bezeichnung", "Beratungsgespräch", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.Status}", "Status", "Geplant", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.StartsAt}", "Beginn", "16.09.2026 10:30", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.EndsAt}", "Ende", "16.09.2026 11:30", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.ReminderChannel}", "Erinnerungsart", "Lokal", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.ReminderMinutesBefore}", "Erinnerung vorher in Minuten", "15", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.Description}", "Beschreibung", "Besprechung vor Ort.", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.CreatedBy}", "Erstellt von", "Team Kosmos", "Aktuelles Meeting", contexts=("meetings",)),
)
SITE_PLACEHOLDERS = (
    TemplatePlaceholder("${Site.Uuid}", "UUID", "2a897ef0-2308-4d97-a6ba-a5e41ff30c93", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.Domain}", "Domain", "beispiel.de", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.HomeUrl}", "Homepage-URL", "https://beispiel.de", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.SiteUrl}", "WordPress-URL", "https://beispiel.de/wp", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.Status}", "Status", "Verifiziert", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.WordpressVersion}", "WordPress-Version", "6.8", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.PhpVersion}", "PHP-Version", "8.3", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.BridgeVersion}", "Bridge-Version", "1.4.0", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.LastSeenAt}", "Zuletzt gesehen", "16.09.2026 10:30", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.RegisteredAt}", "Registriert am", "16.09.2026 09:00", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.VerifiedAt}", "Verifiziert am", "16.09.2026 09:15", "Aktuelle Site", contexts=("sites",)),
)


def document_placeholders(document_type: str, *, email: bool = False) -> tuple[TemplatePlaceholder, ...]:
    namespace, group = DOCUMENT_NAMES[document_type]
    hint = "Nur mit passendem Belegkontext" if email else ""
    return tuple(
        TemplatePlaceholder(
            f"${{{namespace}.{field}}}", label, sample, group, hint=hint,
            contexts=_DOCUMENT_EMAIL_CONTEXTS[document_type] if email else (),
        )
        for field, label, sample in DOCUMENT_FIELDS
        if field in _DOCUMENT_VISIBLE_FIELDS[document_type]
    )


_DOCUMENT_FIELD_CATALOGS = {
    "offers": OFFER_FIELDS,
    "orders": ORDER_FIELDS,
    "invoices": INVOICE_FIELDS,
    "dunnings": DUNNING_FIELDS,
}
_DOCUMENT_FIELD_TOKENS = {
    "offers": {
        "offer_number": "Number",
        "offer_date": "Date",
        "valid_until": "ValidUntil",
        "payment_terms": "PaymentTerms",
    },
    "orders": {
        "order_number": "Number",
        "order_name": "Title",
        "order_date": "Date",
    },
    "invoices": {
        "invoice_number": "Number",
        "invoice_date": "Date",
        "due_date": "DueDate",
        "payment_terms": "PaymentTerms",
    },
    "dunnings": {
        "dunning_number": "Number",
        "dunning_date": "Date",
        "due_date": "DueDate",
        "linked_invoice": "SourceInvoiceNumber",
    },
}


def email_document_placeholders(document_type: str) -> tuple[TemplatePlaceholder, ...]:
    namespace, group = DOCUMENT_NAMES[document_type]
    contexts = _DOCUMENT_EMAIL_CONTEXTS[document_type]
    return _prefer_placeholder_definitions(
        document_placeholders(document_type, email=True),
        _catalog_placeholders(
            namespace,
            group,
            _DOCUMENT_FIELD_CATALOGS[document_type],
            contexts,
            profile_keys=True,
            token_fields=_DOCUMENT_FIELD_TOKENS[document_type],
        ),
    )


def pdf_customer_placeholders(document_type: str) -> tuple[TemplatePlaceholder, ...]:
    bank = INVOICE_CUSTOMER_BANK_PLACEHOLDERS if document_type == "invoices" else ()
    return (*CUSTOMER_PLACEHOLDERS, *bank)


def pdf_placeholders(document_type: str) -> tuple[TemplatePlaceholder, ...]:
    notes = (TemplatePlaceholder("${Offer.Notes}", "Anmerkungen", "Individuelle Anmerkungen zum Angebot.", "Aktuelles Angebot"),) if document_type == "offers" else ()
    return (*document_placeholders(document_type), *notes, *CONTACT_PLACEHOLDERS, *pdf_customer_placeholders(document_type), *COMPANY_PLACEHOLDERS)


def email_placeholders() -> tuple[TemplatePlaceholder, ...]:
    return (
        *EMAIL_CUSTOMER_PLACEHOLDERS,
        *EMAIL_CONTACT_PLACEHOLDERS,
        *LEAD_PLACEHOLDERS,
        *CASE_PLACEHOLDERS,
        *email_document_placeholders("offers"),
        *email_document_placeholders("orders"),
        *email_document_placeholders("invoices"),
        *email_document_placeholders("dunnings"),
        *RECURRING_INVOICE_PLACEHOLDERS,
        *TASK_PLACEHOLDERS,
        *CALL_PLACEHOLDERS,
        *MEETING_PLACEHOLDERS,
        *SITE_PLACEHOLDERS,
        *COMPANY_PLACEHOLDERS,
        *USER_PLACEHOLDERS,
        EMAIL_SIGNATURE_PLACEHOLDER,
    )


def profile_placeholders(namespace: str, values: dict[str, str], *, document_type: str = "") -> dict[str, str]:
    definitions = CONTACT_PLACEHOLDERS if namespace == "Contact" else pdf_customer_placeholders(document_type)
    return {item.token: values.get(item.profile_key, "") for item in definitions if item.profile_key}


def company_template_values() -> dict[str, str]:
    settings = get_settings()
    return {f"Company.{name}": getattr(settings, f"finance_company_{attribute}") for name, attribute in (
        ("Name", "name"), ("Street", "street"), ("PostalCode", "postal_code"), ("City", "city"),
        ("Phone", "phone"), ("Email", "email"), ("Iban", "iban"), ("Bic", "bic"), ("TaxId", "tax_id"))}


def contact_greeting(*, name: str, salutation: str = "", last_name: str = "", letter_salutation: str = "") -> str:
    surname = last_name.strip() or (name.strip().split(" ")[-1] if name.strip() else "")
    if letter_salutation.strip():
        greeting = letter_salutation.strip().rstrip(",")
        if surname and greeting.casefold().startswith("sehr geehrt") and not greeting.casefold().endswith((surname.casefold(), "herren")):
            greeting = f"{greeting} {surname}"
        return greeting
    if surname and salutation.strip().casefold() == "herr":
        return f"Sehr geehrter Herr {surname}"
    if surname and salutation.strip().casefold() == "frau":
        return f"Sehr geehrte Frau {surname}"
    return f"Guten Tag {name.strip()}".strip()
