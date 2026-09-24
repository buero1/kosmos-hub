"""Global field-layout definitions exposed from module directory pages."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.customer_directory import CONTACT_FIELDS_LAYOUT_KEY, CUSTOMER_FIELDS_LAYOUT_KEY
from app.services.hub_case_field_catalog import HUB_CASE_FIELDS
from app.services.hub_cases import CASE_FIELDS_LAYOUT_KEY
from app.services.hub_finance import ARTICLE_FIELDS_LAYOUT_KEY, OFFER_FIELDS_LAYOUT_KEY
from app.services.hub_finance_documents import FINANCE_DOCUMENT_MODULES
from app.services.hub_finance_field_catalog import ARTICLE_FIELDS, OFFER_FIELDS
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS
from app.services.hub_leads import LEAD_FIELDS_LAYOUT_KEY
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS
from app.services.zoho_contact_field_catalog import ZOHO_CONTACT_FIELDS


@dataclass(frozen=True)
class ModuleLayoutField:
    key: str
    label: str
    display_type: str


@dataclass(frozen=True)
class ModuleLayoutDefinition:
    layout_key: str
    label: str
    eyebrow: str
    back_href: str
    back_label: str
    fields: tuple[ModuleLayoutField, ...]
    default_show_more_after: str | None = None


def _fields(definitions: tuple[object, ...]) -> tuple[ModuleLayoutField, ...]:
    return tuple(
        ModuleLayoutField(
            key=str(definition.key),
            label=str(definition.label),
            display_type=str(definition.display_type),
        )
        for definition in definitions
    )


def _customer_fields() -> tuple[ModuleLayoutField, ...]:
    excluded = {"billing_postal_code", "billing_city"}
    by_key = {
        field.key: ModuleLayoutField(field.key, field.label, field.display_type)
        for field in ZOHO_ACCOUNT_FIELDS
        if not field.subform_parent and field.key not in excluded
    }
    priority_keys = (
        "customer_name",
        "phone",
        "account_status",
        "customer_type",
        "website",
        "work_domain_login",
        "billing_street",
    )
    ordered = [by_key.pop(key) for key in priority_keys if key in by_key]
    ordered.append(ModuleLayoutField("hub_postal_city", "PLZ Ort", "Hub-Feld"))
    ordered.extend(by_key.values())
    return tuple(ordered)


def _contact_fields() -> tuple[ModuleLayoutField, ...]:
    fields = [
        ModuleLayoutField("contact-name", "Name", "Einzelzeile"),
        ModuleLayoutField("customer_name", "Kunde-Name", "Link"),
    ]
    for definition in ZOHO_CONTACT_FIELDS:
        fields.append(ModuleLayoutField(definition.key, definition.label, definition.display_type))
        if definition.key == "title":
            fields.append(ModuleLayoutField("contact-position", "Position", "Einzelzeile"))
    return tuple(fields)


_LAYOUTS = {
    CUSTOMER_FIELDS_LAYOUT_KEY: ModuleLayoutDefinition(
        layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
        label="Kunden-Layout",
        eyebrow="CRM directory",
        back_href="/customers",
        back_label="Zurück zu Customers",
        fields=_customer_fields(),
        default_show_more_after="hub_postal_city",
    ),
    CONTACT_FIELDS_LAYOUT_KEY: ModuleLayoutDefinition(
        layout_key=CONTACT_FIELDS_LAYOUT_KEY,
        label="Kontakt-Layout",
        eyebrow="Hub-Daten",
        back_href="/contacts",
        back_label="Zurück zu Kontakte",
        fields=_contact_fields(),
        default_show_more_after="assistant_phone",
    ),
    LEAD_FIELDS_LAYOUT_KEY: ModuleLayoutDefinition(
        layout_key=LEAD_FIELDS_LAYOUT_KEY,
        label="Lead-Layout",
        eyebrow="Hub-Daten",
        back_href="/leads",
        back_label="Zurück zu Leads",
        fields=_fields(HUB_LEAD_FIELDS),
    ),
    CASE_FIELDS_LAYOUT_KEY: ModuleLayoutDefinition(
        layout_key=CASE_FIELDS_LAYOUT_KEY,
        label="Fall-Layout",
        eyebrow="Hub-Daten",
        back_href="/cases",
        back_label="Zurück zu Fälle",
        fields=_fields(HUB_CASE_FIELDS),
    ),
    ARTICLE_FIELDS_LAYOUT_KEY: ModuleLayoutDefinition(
        layout_key=ARTICLE_FIELDS_LAYOUT_KEY,
        label="Artikel-Layout",
        eyebrow="Finance",
        back_href="/finance/articles",
        back_label="Zurück zu Artikel",
        fields=_fields(ARTICLE_FIELDS),
    ),
    OFFER_FIELDS_LAYOUT_KEY: ModuleLayoutDefinition(
        layout_key=OFFER_FIELDS_LAYOUT_KEY,
        label="Angebots-Layout",
        eyebrow="Finance",
        back_href="/finance/offers",
        back_label="Zurück zu Angebote",
        fields=_fields(tuple(field for field in OFFER_FIELDS if field.section == "fields")),
    ),
    **{
        module.layout_key: ModuleLayoutDefinition(
            layout_key=module.layout_key,
            label=f"{module.singular}-Layout",
            eyebrow="Finance",
            back_href=f"/finance/{module.key}",
            back_label=f"Zurück zu {module.label}",
            fields=_fields(module.fields),
        )
        for module in FINANCE_DOCUMENT_MODULES.values()
    },
}


def get_module_layout_definition(layout_key: str) -> ModuleLayoutDefinition | None:
    return _LAYOUTS.get(layout_key)


def module_layout_definitions() -> tuple[ModuleLayoutDefinition, ...]:
    return tuple(_LAYOUTS.values())
