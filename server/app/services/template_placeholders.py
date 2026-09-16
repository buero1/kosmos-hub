"""Canonical, user-facing placeholders shared by email and finance PDFs."""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.services.hub_case_field_catalog import HUB_CASE_FIELDS
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS


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

CUSTOMER_PLACEHOLDERS = (
    TemplatePlaceholder("${Customer.Name}", "Kundenname", "Schreinerei Muster", "Kunde"),
    TemplatePlaceholder("${Customer.CustomerNumber}", "Kundennummer", "10042", "Kunde", "customer_number"),
    TemplatePlaceholder("${Customer.BillingStreet}", "Rechnungsstraße", "Musterstraße 12", "Kunde", "billing_street"),
    TemplatePlaceholder("${Customer.BillingPostalCode}", "Rechnungs-PLZ", "80331", "Kunde", "billing_postal_code"),
    TemplatePlaceholder("${Customer.BillingCity}", "Rechnungsort", "München", "Kunde", "billing_city"),
    TemplatePlaceholder("${Customer.Phone}", "Telefon", "089 123456", "Kunde", "phone"),
    TemplatePlaceholder("${Customer.Website}", "Website", "https://beispiel.de", "Kunde", "website"),
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
        "Boolesch": "Ja",
        "Datum": "16.09.2026",
        "DatumZeit": "16.09.2026 10:30",
        "Datum und Uhrzeit": "16.09.2026 10:30",
        "E-Mail": "max@beispiel.de",
        "Ganzzahl": "1",
        "Telefon": "+49 89 123456",
        "URL": "https://beispiel.de",
    }.get(display_type, "Beispiel")


def _catalog_placeholders(namespace: str, group: str, fields: tuple[object, ...], contexts: tuple[str, ...]) -> tuple[TemplatePlaceholder, ...]:
    return tuple(
        TemplatePlaceholder(
            f"${{{namespace}.{_pascal_case(field.key)}}}",
            field.label,
            _catalog_sample(field.display_type),
            group,
            contexts=contexts,
        )
        for field in fields
    )


LEAD_PLACEHOLDERS = (
    TemplatePlaceholder("${Lead.Greeting}", "Briefanrede", "Sehr geehrter Herr Muster", "Aktueller Lead", contexts=_LEAD_EMAIL_CONTEXTS),
    *_catalog_placeholders("Lead", "Aktueller Lead", HUB_LEAD_FIELDS, _LEAD_EMAIL_CONTEXTS),
)
CASE_PLACEHOLDERS = _catalog_placeholders("Case", "Aktueller Fall", HUB_CASE_FIELDS, ("cases", "tasks"))
RECURRING_INVOICE_PLACEHOLDERS = (
    TemplatePlaceholder("${RecurringInvoice.Name}", "Bezeichnung", "Homepage-Betreuung", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.Status}", "Status", "Aktiv", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.StartDate}", "Startdatum", "16.09.2026", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.EndDate}", "Enddatum", "16.09.2027", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.Interval}", "Rhythmus", "Monatlich", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.NextInvoiceDate}", "Nächstes Rechnungsdatum", "01.10.2026", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
    TemplatePlaceholder("${RecurringInvoice.PaymentDue}", "Zahlungsziel", "14 Tage", "Aktuelle periodische Rechnung", contexts=("recurring-invoices",)),
)
TASK_PLACEHOLDERS = (
    TemplatePlaceholder("${Task.Title}", "Bezeichnung", "Unterlagen prüfen", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.Status}", "Status", "Geplant", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.DueAt}", "Fällig am", "16.09.2026 10:30", "Aktuelle Aufgabe", contexts=("tasks",)),
    TemplatePlaceholder("${Task.Description}", "Beschreibung", "Bitte Unterlagen prüfen.", "Aktuelle Aufgabe", contexts=("tasks",)),
)
CALL_PLACEHOLDERS = (
    TemplatePlaceholder("${Call.Title}", "Bezeichnung", "Rückruf", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.Status}", "Status", "Geplant", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.Direction}", "Richtung", "Ausgehend", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.StartsAt}", "Beginn", "16.09.2026 10:30", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.EndsAt}", "Ende", "16.09.2026 11:00", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.DurationMinutes}", "Dauer in Minuten", "30", "Aktueller Anruf", contexts=("calls",)),
    TemplatePlaceholder("${Call.Description}", "Beschreibung", "Telefonische Abstimmung.", "Aktueller Anruf", contexts=("calls",)),
)
MEETING_PLACEHOLDERS = (
    TemplatePlaceholder("${Meeting.Title}", "Bezeichnung", "Beratungsgespräch", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.Status}", "Status", "Geplant", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.StartsAt}", "Beginn", "16.09.2026 10:30", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.EndsAt}", "Ende", "16.09.2026 11:30", "Aktuelles Meeting", contexts=("meetings",)),
    TemplatePlaceholder("${Meeting.Description}", "Beschreibung", "Besprechung vor Ort.", "Aktuelles Meeting", contexts=("meetings",)),
)
SITE_PLACEHOLDERS = (
    TemplatePlaceholder("${Site.Domain}", "Domain", "beispiel.de", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.HomeUrl}", "Homepage-URL", "https://beispiel.de", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.SiteUrl}", "WordPress-URL", "https://beispiel.de/wp", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.Status}", "Status", "Verifiziert", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.WordpressVersion}", "WordPress-Version", "6.8", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.PhpVersion}", "PHP-Version", "8.3", "Aktuelle Site", contexts=("sites",)),
    TemplatePlaceholder("${Site.LastSeenAt}", "Zuletzt gesehen", "16.09.2026 10:30", "Aktuelle Site", contexts=("sites",)),
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


def pdf_placeholders(document_type: str) -> tuple[TemplatePlaceholder, ...]:
    return (*document_placeholders(document_type), *CONTACT_PLACEHOLDERS, *CUSTOMER_PLACEHOLDERS, *COMPANY_PLACEHOLDERS)


def email_placeholders() -> tuple[TemplatePlaceholder, ...]:
    return (
        *tuple(replace(item, contexts=_CUSTOMER_EMAIL_CONTEXTS) for item in CUSTOMER_PLACEHOLDERS),
        *tuple(replace(item, contexts=_CONTACT_EMAIL_CONTEXTS) for item in CONTACT_PLACEHOLDERS),
        *LEAD_PLACEHOLDERS,
        *CASE_PLACEHOLDERS,
        *document_placeholders("offers", email=True),
        *document_placeholders("orders", email=True),
        *document_placeholders("invoices", email=True),
        *document_placeholders("dunnings", email=True),
        *RECURRING_INVOICE_PLACEHOLDERS,
        *TASK_PLACEHOLDERS,
        *CALL_PLACEHOLDERS,
        *MEETING_PLACEHOLDERS,
        *SITE_PLACEHOLDERS,
        *COMPANY_PLACEHOLDERS,
        EMAIL_SIGNATURE_PLACEHOLDER,
    )


def profile_placeholders(namespace: str, values: dict[str, str]) -> dict[str, str]:
    definitions = CONTACT_PLACEHOLDERS if namespace == "Contact" else CUSTOMER_PLACEHOLDERS
    return {item.token: values.get(item.profile_key, "") for item in definitions if item.profile_key}


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
