"""Safe review and explicit linking of imported CRM customers to Hub sites."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerZohoEmail
from app.models.module_layout import ModuleLayout
from app.models.site import Site
from app.services.hub_cases import HubCaseListEntry, HubCaseService
from app.services.module_layouts import ModuleLayoutService
from app.services.zoho_contact_field_catalog import ZOHO_CONTACT_FIELDS
from app.services.zoho_crm import ZohoCrmService


CUSTOMER_FIELDS_LAYOUT_KEY = "customer-fields"
CONTACT_FIELDS_LAYOUT_KEY = "contact-fields"


@dataclass(frozen=True)
class CustomerDirectoryEntry:
    customer: Customer
    account_status: str | None
    linked_sites: tuple[Site, ...]
    exact_match_candidate: Site | None


@dataclass(frozen=True)
class CustomerProfileField:
    label: str
    value: str | None
    key: str = ""
    display_type: str = "Einzelzeile"
    form_value: str = ""
    options: tuple[tuple[str, str], ...] = ()
    editable: bool = False
    sensitive: bool = False
    required: bool = False


@dataclass(frozen=True)
class CustomerProfileFieldTab:
    """A named, filtered view of the customer fields without duplicating their data."""

    key: str
    label: str
    fields: tuple[CustomerProfileField, ...]


@dataclass(frozen=True)
class CustomerProfileSubformRow:
    id: str | None
    fields: tuple[CustomerProfileField, ...]


@dataclass(frozen=True)
class CustomerProfileSubform:
    key: str
    label: str
    fields: tuple[CustomerProfileField, ...]
    records: tuple[CustomerProfileSubformRow, ...]


@dataclass(frozen=True)
class CustomerDirectoryDetail:
    entry: CustomerDirectoryEntry
    profile_fields: tuple[CustomerProfileField, ...]
    contacts: tuple["CustomerContactProfile", ...]
    cases: tuple[HubCaseListEntry, ...] = ()
    wordpress_admin_site: Site | None = None
    editable_profile_fields: tuple[CustomerProfileField, ...] = ()
    subforms: tuple[CustomerProfileSubform, ...] = ()
    display_profile_fields: tuple[CustomerProfileField, ...] = ()
    summary_profile_fields: tuple[CustomerProfileField, ...] = ()
    following_profile_fields: tuple[CustomerProfileField, ...] = ()
    profile_field_tabs: tuple[CustomerProfileFieldTab, ...] = ()


@dataclass(frozen=True)
class CustomerContactProfile:
    id: int
    name: str
    salutation: str | None
    title: str | None
    email: str | None
    secondary_email: str | None
    third_email: str | None
    phone: str | None
    alternate_phone: str | None
    private_phone: str | None
    mobile: str | None
    searchable_values: tuple[str, ...]
    phone_values: tuple[str, ...]
    is_hub_contact: bool = False


@dataclass(frozen=True)
class CustomerContactDetail:
    customer: Customer | None
    contact: CustomerContact
    name: str
    profile_fields: tuple[CustomerProfileField, ...]
    editable_profile_fields: tuple[CustomerProfileField, ...] = ()
    display_profile_fields: tuple[CustomerProfileField, ...] = ()
    summary_profile_fields: tuple[CustomerProfileField, ...] = ()
    following_profile_fields: tuple[CustomerProfileField, ...] = ()
    profile_field_tabs: tuple[CustomerProfileFieldTab, ...] = ()


@dataclass(frozen=True)
class CustomerContactDirectoryEntry:
    customer: Customer | None
    contact: CustomerContactProfile


class CustomerDirectoryService:
    """Presents CRM customers without ever assigning sites automatically."""

    _CONTACT_PROFILE_FIELD_TAB_GROUPS = (
        (
            "contact-details",
            "Kontakt",
            frozenset((
                "name",
                "anrede",
                "briefanrede",
                "vorname",
                "nachname",
                "titel",
                "position",
                "funktion",
                "e-mail",
                "zweite e-mail-adresse",
                "dritte e-mail-adresse",
                "tel.",
                "telefon",
                "telefon alternativ",
                "telefon privat",
                "telefon sekr.",
                "mobil",
            )),
        ),
        (
            "postal-address",
            "Postadresse",
            frozenset((
                "postadresse straße",
                "postadresse plz",
                "postadresse stadt",
                "postadresse bundesland",
                "postadresse land",
            )),
        ),
        ("additional", "Weitere Daten", frozenset(("status kunde", "tag"))),
    )

    _PROFILE_FIELD_TAB_GROUPS = (
        (
            "bank-details",
            "Bankverbindung",
            frozenset((
                "bank",
                "show_bank_details",
                "bic",
                "iban",
                "account_holder",
                "sepa_grant_type",
                "sepa_grant_date",
            )),
        ),
        ("dialfire", "Dialfire", frozenset((
            "dialfire_campaign_name",
            "dialfire_campaign_stage",
            "dialfire_follow_up_at",
            "dialfire_api_token",
            "dialfire_comment",
            "dialfire_result",
            "dialfire_id",
            "dialfire_campaign_id",
            "dialfire_season_time",
            "dialfire_status",
            "dialfire_follow_up_owner",
            "dialfire_follow_up_formula",
            "dialfire_follow_up_note",
        ))),
        ("appointments", "Termin", frozenset((
            "duration_minutes",
            "google_calendar",
            "appointment_reminder",
            "appointment_at",
        ))),
    )

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_entries(
        self,
        *,
        query: str = "",
        status: str | None = None,
        industry: str | None = None,
        unread_email_only: bool = False,
        include_sensitive: bool = False,
    ) -> list[CustomerDirectoryEntry]:
        customers = list(self.db.scalars(select(Customer).order_by(Customer.name.asc(), Customer.id.asc())).all())
        linked_by_customer, unlinked_by_domain = self._site_maps()
        contacts_by_customer = self._contact_profiles_by_customer()
        unread_customer_ids = set(
            self.db.scalars(
                select(CustomerZohoEmail.customer_id).where(
                    CustomerZohoEmail.direction == "inbound",
                    CustomerZohoEmail.is_unread.is_(True),
                    CustomerZohoEmail.mailbox_state == "active",
                )
            ).all()
        ) if unread_email_only else set()

        needle = query.strip().casefold()
        phone_query_forms = self._phone_search_forms(query)
        entries: list[CustomerDirectoryEntry] = []
        for customer in customers:
            profile_fields = self._profile_fields(customer, include_sensitive=include_sensitive)
            contacts = contacts_by_customer.get(customer.id, ())
            entry = self._build_entry(customer, linked_by_customer, unlinked_by_domain, profile_fields=profile_fields)
            if status is not None and (entry.account_status or "").casefold() != status.casefold():
                continue
            customer_industry = self._profile_field_value(profile_fields, "Branche")
            if industry is not None and (customer_industry or "").casefold() != industry.casefold():
                continue
            if unread_email_only and customer.id not in unread_customer_ids:
                continue
            searchable_values = [customer.name, customer.external_id, customer.website_domain, customer.zoho_id]
            searchable_values.extend(f"{field.label} {field.value or ''}" for field in profile_fields)
            searchable_values.extend(
                value for contact in contacts for value in contact.searchable_values
            )
            searchable = " ".join(
                value
                for value in searchable_values
                if value
            ).casefold()
            phone_values = [
                field.value
                for field in profile_fields
                if self._is_phone_field(field.label) and field.value
            ]
            phone_values.extend(
                value for contact in contacts for value in contact.phone_values
            )
            if needle and needle not in searchable and not self._matches_phone_query(phone_query_forms, phone_values):
                continue
            entries.append(entry)
        return entries

    def list_industries(self) -> list[str]:
        """Return the available Zoho account industries for the customer filter."""
        industries_by_key: dict[str, str] = {}
        for customer in self.db.scalars(select(Customer).order_by(Customer.name.asc(), Customer.id.asc())).all():
            industry = self._profile_field_value(self._profile_fields(customer), "Branche")
            if industry:
                industries_by_key.setdefault(industry.casefold(), industry)
        return sorted(industries_by_key.values(), key=str.casefold)

    def get_detail(self, *, customer_id: int, include_sensitive: bool = False) -> CustomerDirectoryDetail | None:
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            return None
        linked_by_customer, unlinked_by_domain = self._site_maps()
        profile = self._profile_data(customer)
        profile_fields = self._profile_fields_from_data(profile, include_sensitive=include_sensitive)
        display_profile_fields = self._profile_field_display_layout(profile_fields)
        summary_profile_fields, following_profile_fields = self._split_profile_field_summary(
            display_profile_fields
        )
        profile_field_tabs = self._profile_field_tabs(display_profile_fields)
        editable_profile_fields = self._ordered_editable_profile_fields(
            display_profile_fields,
            tuple(field for field in profile_fields if field.editable),
        )
        entry = self._build_entry(customer, linked_by_customer, unlinked_by_domain, profile_fields=profile_fields)
        return CustomerDirectoryDetail(
            entry=entry,
            profile_fields=profile_fields,
            contacts=self._contact_profiles(customer),
            cases=HubCaseService(db=self.db, cipher=self.cipher).list_cases_for_customer(customer_id=customer.id),
            wordpress_admin_site=self._wordpress_admin_site(entry=entry, profile_fields=profile_fields),
            editable_profile_fields=editable_profile_fields,
            subforms=self._profile_subforms(profile, include_sensitive=include_sensitive),
            display_profile_fields=display_profile_fields,
            summary_profile_fields=summary_profile_fields,
            following_profile_fields=following_profile_fields,
            profile_field_tabs=profile_field_tabs,
        )

    def get_contact_detail(self, *, customer_id: int, contact_id: int) -> CustomerContactDetail | None:
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            return None
        contact = self.db.scalar(
            select(CustomerContact).where(
                CustomerContact.id == contact_id,
                CustomerContact.customer_id == customer.id,
            )
        )
        if contact is None:
            return None
        return self._build_contact_detail(contact)

    def get_contact_detail_by_id(self, *, contact_id: int) -> CustomerContactDetail | None:
        contact = self.db.get(CustomerContact, contact_id)
        if contact is None:
            return None
        return self._build_contact_detail(contact)

    def create_hub_contact(
        self,
        *,
        customer_id: int | None,
        submitted_values: dict[str, str],
    ) -> CustomerContact:
        customer = None
        if customer_id is not None:
            customer = self.db.get(Customer, customer_id)
            if customer is None or not customer.is_visible:
                raise ValueError("Der ausgewählte Kunde ist nicht verfügbar.")
        contact = CustomerContact(
            customer=customer,
            zoho_id=None,
            encrypted_profile_json=self.cipher.encrypt(
                json.dumps(self._hub_contact_profile(self._hub_contact_values(submitted_values, require_salutation=True)), ensure_ascii=False)
            ),
        )
        self.db.add(contact)
        self.db.flush()
        return contact

    def update_hub_contact(self, *, contact_id: int, submitted_values: dict[str, str]) -> CustomerContact:
        contact = self.db.get(CustomerContact, contact_id)
        if contact is None:
            raise ValueError("Der Kontakt wurde nicht gefunden.")
        if contact.zoho_id:
            raise ValueError("Dieser Zoho-Kontakt wird über die Zoho-Bearbeitung aktualisiert.")
        contact.encrypted_profile_json = self.cipher.encrypt(
            json.dumps(self._hub_contact_profile(self._hub_contact_values(submitted_values, require_salutation=False)), ensure_ascii=False)
        )
        self.db.flush()
        return contact

    def set_hub_contact_customer(self, *, contact_id: int, customer_id: int | None) -> CustomerContact:
        contact = self.db.get(CustomerContact, contact_id)
        if contact is None:
            raise ValueError("Der Kontakt wurde nicht gefunden.")
        if contact.zoho_id:
            raise ValueError("Die Verknüpfung eines Zoho-Kontakts wird durch Zoho CRM verwaltet.")
        if customer_id is None:
            contact.customer = None
        else:
            customer = self.db.get(Customer, customer_id)
            if customer is None or not customer.is_visible:
                raise ValueError("Der ausgewählte Kunde ist nicht verfügbar.")
            contact.customer = customer
        self.db.flush()
        return contact

    def delete_contact_from_hub(self, *, contact_id: int) -> CustomerContact:
        contact = self.db.get(CustomerContact, contact_id)
        if contact is None:
            raise ValueError("Der Kontakt wurde nicht gefunden.")
        self.db.delete(contact)
        self.db.flush()
        return contact

    def _build_contact_detail(self, contact: CustomerContact) -> CustomerContactDetail:
        profile_fields = self._contact_profile_fields(contact)
        display_profile_fields = self._contact_profile_field_display_layout(contact, profile_fields)
        summary_profile_fields, following_profile_fields = self._split_contact_profile_field_summary(
            display_profile_fields
        )
        editable_profile_fields = self._ordered_editable_profile_fields(
            display_profile_fields,
            self._contact_editable_profile_fields(contact, profile_fields),
        )
        return CustomerContactDetail(
            customer=contact.customer,
            contact=contact,
            name=next((field.value for field in profile_fields if field.label == "Name" and field.value), "Hub Kontakt"),
            profile_fields=profile_fields,
            editable_profile_fields=editable_profile_fields,
            display_profile_fields=display_profile_fields,
            summary_profile_fields=summary_profile_fields,
            following_profile_fields=following_profile_fields,
            profile_field_tabs=self._contact_profile_field_tabs(profile_fields),
        )

    def list_contact_entries(self) -> tuple[CustomerContactDirectoryEntry, ...]:
        """Return all Hub contacts, including contacts without a customer or Zoho link."""
        entries: list[CustomerContactDirectoryEntry] = []
        rows = self.db.execute(
            select(CustomerContact, Customer)
            .outerjoin(Customer, Customer.id == CustomerContact.customer_id)
        ).all()
        for stored_contact, customer in rows:
            profiles = self._contact_profiles_from_records([stored_contact])
            if profiles:
                entries.append(CustomerContactDirectoryEntry(customer=customer, contact=profiles[0]))
        return tuple(
            sorted(
                entries,
                key=lambda entry: (entry.contact.name.casefold(), entry.customer.name.casefold() if entry.customer else ""),
            )
        )

    @staticmethod
    def _hub_contact_values(submitted_values: dict[str, str], *, require_salutation: bool) -> dict[str, str]:
        values: dict[str, str] = {}
        for definition in ZOHO_CONTACT_FIELDS:
            value = str(submitted_values.get(f"contact_field__{definition.key}") or "").strip()
            if len(value) > 1_000:
                raise ValueError(f"{definition.label} ist zu lang.")
            if (definition.required or (require_salutation and definition.key == "salutation")) and not value:
                raise ValueError(f"{definition.label} ist erforderlich.")
            values[definition.key] = value
        return values

    @staticmethod
    def _hub_contact_profile(values: dict[str, str]) -> dict[str, object]:
        name = " ".join(value for value in (values.get("first_name"), values.get("last_name")) if value)
        fields = {"Name": name or "Hub Kontakt"}
        fields.update({definition.label: values.get(definition.key, "") for definition in ZOHO_CONTACT_FIELDS})
        return {
            "source": "hub",
            "record_id": None,
            "account_id": None,
            "modified_time": None,
            "synced_at": None,
            "fields": fields,
        }

    def link_exact_match(self, *, customer_id: int, site_id: int) -> tuple[Customer, Site]:
        customer = self.db.get(Customer, customer_id)
        site = self.db.get(Site, site_id)
        if customer is None or site is None:
            raise ValueError("The customer or site no longer exists.")
        if site.customer_id is not None:
            raise ValueError("This site is already linked to a customer and was not changed.")

        expected_domain = customer.website_domain
        actual_domain = ZohoCrmService.normalize_website_domain(site.domain)
        if not expected_domain or actual_domain != expected_domain:
            raise ValueError("Only an exact Zoho website-domain match can be linked from this review screen.")

        matching_unlinked_sites = [
            current_site
            for current_site in self.db.scalars(select(Site).where(Site.customer_id.is_(None))).all()
            if ZohoCrmService.normalize_website_domain(current_site.domain) == expected_domain
        ]
        if len(matching_unlinked_sites) != 1 or matching_unlinked_sites[0].id != site.id:
            raise ValueError("This website-domain match is ambiguous and requires a later manual review workflow.")

        site.customer_id = customer.id
        self.db.flush()
        return customer, site

    def _site_maps(self) -> tuple[dict[int, list[Site]], dict[str, list[Site]]]:
        sites = list(self.db.scalars(select(Site).order_by(Site.domain.asc(), Site.id.asc())).all())
        linked_by_customer: dict[int, list[Site]] = {}
        unlinked_by_domain: dict[str, list[Site]] = {}
        for site in sites:
            if site.customer_id is not None:
                linked_by_customer.setdefault(site.customer_id, []).append(site)
                continue
            normalized_domain = ZohoCrmService.normalize_website_domain(site.domain)
            if normalized_domain:
                unlinked_by_domain.setdefault(normalized_domain, []).append(site)
        return linked_by_customer, unlinked_by_domain

    def _build_entry(
        self,
        customer: Customer,
        linked_by_customer: dict[int, list[Site]],
        unlinked_by_domain: dict[str, list[Site]],
        *,
        profile_fields: tuple[CustomerProfileField, ...] | None = None,
    ) -> CustomerDirectoryEntry:
        fields = profile_fields if profile_fields is not None else self._profile_fields(customer)
        candidate_sites = unlinked_by_domain.get(customer.website_domain or "", [])
        account_status = customer.zoho_status
        if account_status is None and customer.is_visible:
            account_status = next((field.value for field in fields if field.label == "Status"), None)
        return CustomerDirectoryEntry(
            customer=customer,
            account_status=account_status,
            linked_sites=tuple(linked_by_customer.get(customer.id, [])),
            exact_match_candidate=candidate_sites[0] if len(candidate_sites) == 1 else None,
        )

    @staticmethod
    def _wordpress_admin_site(
        *,
        entry: CustomerDirectoryEntry,
        profile_fields: tuple[CustomerProfileField, ...],
    ) -> Site | None:
        """Only offer the admin shortcut for the linked Hub site of this login domain."""
        work_domain_login = next(
            (field.value for field in profile_fields if field.key == "work_domain_login"),
            None,
        )
        login_domain = ZohoCrmService.normalize_website_domain(work_domain_login)
        if not login_domain:
            return None
        matching_sites = tuple(
            site
            for site in entry.linked_sites
            if ZohoCrmService.normalize_website_domain(site.domain) == login_domain
        )
        return matching_sites[0] if len(matching_sites) == 1 else None

    def _profile_fields(self, customer: Customer, *, include_sensitive: bool = False) -> tuple[CustomerProfileField, ...]:
        return self._profile_fields_from_data(self._profile_data(customer), include_sensitive=include_sensitive)

    def _profile_data(self, customer: Customer) -> dict[str, object]:
        if not customer.encrypted_profile_json:
            return {}
        try:
            profile = json.loads(self.cipher.decrypt(customer.encrypted_profile_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return profile if isinstance(profile, dict) else {}

    def _profile_fields_from_data(
        self,
        profile: dict[str, object],
        *,
        include_sensitive: bool,
    ) -> tuple[CustomerProfileField, ...]:
        values = profile.get("fields")
        metadata = profile.get("field_metadata")
        if not isinstance(values, dict):
            return ()
        metadata_by_label = {
            str(definition.get("label")): (str(key), definition)
            for key, definition in metadata.items()
            if isinstance(metadata, dict) and isinstance(key, str) and isinstance(definition, dict)
        } if isinstance(metadata, dict) else {}
        fields: list[CustomerProfileField] = []
        for label, raw_value in values.items():
            key, definition = metadata_by_label.get(str(label), (str(label), {}))
            sensitive = bool(definition.get("sensitive")) if isinstance(definition, dict) else False
            if sensitive and not include_sensitive:
                continue
            fields.append(self._profile_field(str(label), key, raw_value, definition, sensitive=sensitive))
        return tuple(fields)

    def _profile_field_display_layout(
        self,
        profile_fields: tuple[CustomerProfileField, ...],
    ) -> tuple[CustomerProfileField, ...]:
        """Place the core customer data first and combine postal code with city for the Hub."""
        visible_fields = tuple(field for field in profile_fields if field.value)
        fields_by_key = {field.key: field for field in visible_fields if field.key}
        postal_code = fields_by_key.get("billing_postal_code")
        city = fields_by_key.get("billing_city")
        postal_city_value = " ".join(
            value for value in (postal_code.value if postal_code else None, city.value if city else None) if value
        )
        postal_city = (
            CustomerProfileField(label="PLZ Ort", value=postal_city_value, key="hub_postal_city", display_type="Hub-Feld")
            if postal_city_value
            else None
        )

        priority_keys = (
            "customer_name",
            "phone",
            "account_status",
            "customer_type",
            "website",
            "work_domain_login",
            "billing_street",
        )
        priority_fields = tuple(fields_by_key[key] for key in priority_keys if key in fields_by_key)
        if postal_city is not None:
            priority_fields += (postal_city,)
        if "send_options_to_wordpress" in fields_by_key:
            priority_fields += (fields_by_key["send_options_to_wordpress"],)

        placed_keys = set(priority_keys) | {"send_options_to_wordpress"}
        if postal_city is not None:
            placed_keys.update(("billing_postal_code", "billing_city"))
        default_fields = priority_fields + tuple(field for field in visible_fields if field.key not in placed_keys)
        fields_by_key = {field.key: field for field in default_fields if field.key}
        ordered_keys = ModuleLayoutService(db=self.db).ordered_keys(
            layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
            default_keys=tuple(fields_by_key),
        )
        return tuple(fields_by_key[key] for key in ordered_keys)

    @staticmethod
    def _ordered_editable_profile_fields(
        display_profile_fields: tuple[CustomerProfileField, ...],
        editable_profile_fields: tuple[CustomerProfileField, ...],
    ) -> tuple[CustomerProfileField, ...]:
        """Keep edit controls aligned with the saved reading layout, then append hidden empty fields."""
        editable_by_key = {
            field.key: field
            for field in editable_profile_fields
            if field.key
        }
        ordered: list[CustomerProfileField] = []
        for field in display_profile_fields:
            editable = editable_by_key.pop(field.key, None)
            if editable is not None:
                ordered.append(editable)
        ordered.extend(
            field for field in editable_profile_fields
            if field.key and field.key in editable_by_key
        )
        return tuple(ordered)

    @staticmethod
    def _split_profile_field_summary(
        display_profile_fields: tuple[CustomerProfileField, ...],
    ) -> tuple[tuple[CustomerProfileField, ...], tuple[CustomerProfileField, ...]]:
        """Keep the global field order while ending the summary at the WordPress action."""
        for index, field in enumerate(display_profile_fields):
            if field.key == "send_options_to_wordpress":
                return display_profile_fields[: index + 1], display_profile_fields[index + 1 :]
        return display_profile_fields, ()

    @staticmethod
    def _split_contact_profile_field_summary(
        display_profile_fields: tuple[CustomerProfileField, ...],
    ) -> tuple[tuple[CustomerProfileField, ...], tuple[CustomerProfileField, ...]]:
        """Keep contact details visible through the secretary phone number."""
        for index, field in enumerate(display_profile_fields):
            if field.key == "assistant_phone":
                return display_profile_fields[: index + 1], display_profile_fields[index + 1 :]
        return display_profile_fields, ()

    @classmethod
    def _profile_field_tabs(
        cls,
        display_profile_fields: tuple[CustomerProfileField, ...],
    ) -> tuple[CustomerProfileFieldTab, ...]:
        """Filter the displayed field order into reusable tab views."""
        return tuple(
            CustomerProfileFieldTab(
                key=key,
                label=label,
                fields=tuple(field for field in display_profile_fields if field.key in field_keys),
            )
            for key, label, field_keys in cls._PROFILE_FIELD_TAB_GROUPS
        )

    def _profile_subforms(
        self,
        profile: dict[str, object],
        *,
        include_sensitive: bool,
    ) -> tuple[CustomerProfileSubform, ...]:
        stored_subforms = profile.get("subforms")
        if not isinstance(stored_subforms, dict):
            return ()
        subforms: list[CustomerProfileSubform] = []
        for key, source in stored_subforms.items():
            if not isinstance(key, str) or not isinstance(source, dict):
                continue
            metadata = source.get("metadata")
            if not isinstance(metadata, dict):
                continue
            definitions = tuple(
                self._profile_field(
                    str(definition.get("label") or field_key),
                    str(field_key),
                    None,
                    definition,
                    sensitive=bool(definition.get("sensitive")),
                )
                for field_key, definition in metadata.items()
                if isinstance(field_key, str)
                and isinstance(definition, dict)
                and (include_sensitive or not definition.get("sensitive"))
            )
            records = source.get("records")
            rows: list[CustomerProfileSubformRow] = []
            if isinstance(records, list):
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    values = record.get("values") if isinstance(record.get("values"), dict) else {}
                    row_fields = tuple(
                        self._profile_field(
                            field.label,
                            field.key,
                            values.get(field.key),
                            metadata.get(field.key) if isinstance(metadata.get(field.key), dict) else {},
                            sensitive=field.sensitive,
                        )
                        for field in definitions
                    )
                    rows.append(CustomerProfileSubformRow(id=self._format_profile_value(record.get("id")), fields=row_fields))
            subforms.append(
                CustomerProfileSubform(
                    key=key,
                    label=str(source.get("label") or key),
                    fields=definitions,
                    records=tuple(rows),
                )
            )
        return tuple(subforms)

    def _profile_field(
        self,
        label: str,
        key: str,
        raw_value: object,
        definition: object,
        *,
        sensitive: bool,
    ) -> CustomerProfileField:
        source = definition if isinstance(definition, dict) else {}
        options = source.get("pick_list_values")
        option_rows = options if isinstance(options, list) else ()
        option_values = tuple(
            (str(option.get("value")), str(option.get("label") or option.get("value")))
            for option in option_rows
            if isinstance(option, dict)
            and isinstance(option.get("value"), str)
        )
        text_value = self._format_profile_value(raw_value)
        displayed_value = next((label for value, label in option_values if value == text_value), text_value)
        form_value = next((value for value, label in option_values if label == text_value), text_value)
        return CustomerProfileField(
            label=label,
            value="Geschützt" if sensitive and raw_value is not None else displayed_value,
            key=key,
            display_type=str(source.get("display_type") or "Einzelzeile"),
            form_value="" if sensitive else (form_value or ""),
            options=option_values,
            editable=bool(source.get("editable")),
            sensitive=sensitive,
        )

    @staticmethod
    def _profile_field_value(fields: tuple[CustomerProfileField, ...], label: str) -> str | None:
        label_key = label.casefold()
        return next(
            (field.value for field in fields if field.label.casefold() == label_key and field.value),
            None,
        )

    def _contact_profiles(self, customer: Customer) -> tuple[CustomerContactProfile, ...]:
        stored_contacts = self.db.scalars(
            select(CustomerContact).where(CustomerContact.customer_id == customer.id)
        ).all()
        return self._contact_profiles_from_records(stored_contacts)

    def _contact_profiles_by_customer(self) -> dict[int, tuple[CustomerContactProfile, ...]]:
        contacts_by_customer: dict[int, list[CustomerContact]] = {}
        for contact in self.db.scalars(select(CustomerContact)).all():
            if contact.customer_id is None:
                continue
            contacts_by_customer.setdefault(contact.customer_id, []).append(contact)
        return {
            customer_id: self._contact_profiles_from_records(contacts)
            for customer_id, contacts in contacts_by_customer.items()
        }

    def _contact_profiles_from_records(self, stored_contacts: list[CustomerContact]) -> tuple[CustomerContactProfile, ...]:
        contacts: list[CustomerContactProfile] = []
        for contact in stored_contacts:
            try:
                profile = json.loads(self.cipher.decrypt(contact.encrypted_profile_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            values = profile.get("fields") if isinstance(profile, dict) else None
            if not isinstance(values, dict):
                continue
            formatted_fields = tuple(
                CustomerProfileField(label=str(label), value=self._format_profile_value(value))
                for label, value in values.items()
            )
            contacts.append(
                CustomerContactProfile(
                    id=contact.id,
                    name=self._format_profile_value(values.get("Name")) or "Unnamed Zoho contact",
                    salutation=self._format_profile_value(values.get("Anrede")),
                    title=self._format_profile_value(values.get("Titel")) or self._format_profile_value(values.get("Position")),
                    email=self._format_profile_value(values.get("E-Mail")),
                    secondary_email=self._format_profile_value(values.get("Zweite E-Mail-Adresse")),
                    third_email=self._format_profile_value(values.get("Dritte E-Mail-Adresse")),
                    phone=self._format_profile_value(values.get("Telefon")),
                    alternate_phone=self._format_profile_value(values.get("Telefon alternativ")),
                    private_phone=self._format_profile_value(values.get("Telefon privat")),
                    mobile=self._format_profile_value(values.get("Mobil")),
                    searchable_values=tuple(
                        f"{field.label} {field.value}" for field in formatted_fields if field.value
                    ),
                    phone_values=tuple(
                        field.value
                        for field in formatted_fields
                        if field.value and self._is_phone_field(field.label)
                    ),
                    is_hub_contact=contact.zoho_id is None,
                )
            )
        return tuple(sorted(contacts, key=lambda contact: contact.name.casefold()))

    def _contact_profile_fields(self, contact: CustomerContact) -> tuple[CustomerProfileField, ...]:
        try:
            profile = json.loads(self.cipher.decrypt(contact.encrypted_profile_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        values = profile.get("fields") if isinstance(profile, dict) else None
        if not isinstance(values, dict):
            return ()
        definitions_by_label = {field.label.casefold(): field for field in ZOHO_CONTACT_FIELDS}
        definitions_by_label["telefon"] = next(field for field in ZOHO_CONTACT_FIELDS if field.key == "phone")
        return tuple(
            CustomerProfileField(
                label=str(label),
                value=self._format_profile_value(value),
                key=(
                    definition.key
                    if (definition := definitions_by_label.get(str(label).casefold()))
                    else self._contact_layout_key(str(label))
                ),
                display_type=definition.display_type if definition else "Einzelzeile",
            )
            for label, value in values.items()
        )

    @staticmethod
    def _contact_layout_key(label: str) -> str:
        """Give non-catalogued Zoho fields stable IDs for the global layout."""
        normalized = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
        return f"contact-{normalized or 'field'}"

    def _contact_editable_profile_fields(
        self,
        contact: CustomerContact,
        profile_fields: tuple[CustomerProfileField, ...],
    ) -> tuple[CustomerProfileField, ...]:
        values_by_key = {field.key: field.form_value or field.value or "" for field in profile_fields if field.key}
        name = next((field.value for field in profile_fields if field.label == "Name"), "") or ""
        name_parts = name.rsplit(" ", 1)
        if not values_by_key.get("last_name") and name_parts:
            values_by_key["last_name"] = name_parts[-1]
        if not values_by_key.get("first_name") and len(name_parts) == 2:
            values_by_key["first_name"] = name_parts[0]
        return tuple(
            CustomerProfileField(
                label=definition.label,
                value=values_by_key.get(definition.key) or None,
                key=definition.key,
                display_type=definition.display_type,
                form_value=values_by_key.get(definition.key, ""),
                editable=True,
                required=definition.required,
            )
            for definition in ZOHO_CONTACT_FIELDS
        )

    def _contact_profile_field_display_layout(
        self,
        contact: CustomerContact,
        profile_fields: tuple[CustomerProfileField, ...],
    ) -> tuple[CustomerProfileField, ...]:
        """Show the complete reviewed contact schema for older partial imports too."""
        display_fields = list(profile_fields)
        fields_by_key = {field.key: field for field in display_fields if field.key}
        if contact.customer is not None and "customer_name" not in fields_by_key:
            customer_field = CustomerProfileField(
                label="Kunde-Name",
                value=contact.customer.name if contact.customer is not None else None,
                key="customer_name",
                display_type="Link",
            )
            name_index = next((index for index, field in enumerate(display_fields) if field.label == "Name"), -1)
            display_fields.insert(name_index + 1, customer_field)
            fields_by_key[customer_field.key] = customer_field
        for definition in ZOHO_CONTACT_FIELDS:
            if definition.key in fields_by_key:
                continue
            field = CustomerProfileField(
                label=definition.label,
                value=None,
                key=definition.key,
                display_type=definition.display_type,
            )
            display_fields.append(field)
            fields_by_key[field.key] = field
        layout = self.db.scalar(select(ModuleLayout).where(ModuleLayout.layout_key == CONTACT_FIELDS_LAYOUT_KEY))
        if layout is None:
            return tuple(display_fields)
        ordered_keys = ModuleLayoutService(db=self.db).ordered_keys(
            layout_key=CONTACT_FIELDS_LAYOUT_KEY,
            default_keys=tuple(fields_by_key),
        )
        return tuple(fields_by_key[key] for key in ordered_keys)

    @classmethod
    def _contact_profile_field_tabs(
        cls,
        profile_fields: tuple[CustomerProfileField, ...],
    ) -> tuple[CustomerProfileFieldTab, ...]:
        """Present the same encrypted fields in focused contact views without duplicating data."""
        return tuple(
            CustomerProfileFieldTab(
                key=key,
                label=label,
                fields=tuple(
                    field for field in profile_fields if field.label.casefold() in field_labels
                ),
            )
            for key, label, field_labels in cls._CONTACT_PROFILE_FIELD_TAB_GROUPS
        )

    @staticmethod
    def _is_phone_field(label: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]", "", label.casefold())
        return any(token in normalized for token in ("tel", "telefon", "phone", "mobil", "mobile"))

    @staticmethod
    def _phone_search_forms(value: str) -> tuple[str, ...]:
        digits = re.sub(r"\D", "", value)
        if len(digits) < 3:
            return ()

        national = digits
        if national.startswith("0049"):
            national = f"0{national[4:]}"
        elif national.startswith("49"):
            national = f"0{national[2:]}"

        forms = {digits, national}
        if digits.startswith("0"):
            forms.add(digits[1:])
        if national.startswith("0"):
            forms.add(national[1:])
        return tuple(form for form in forms if form)

    @classmethod
    def _matches_phone_query(cls, query_forms: tuple[str, ...], values: list[str]) -> bool:
        if not query_forms:
            return False
        return any(
            query_form in value_form
            for value in values
            for value_form in cls._phone_search_forms(value)
            for query_form in query_forms
        )

    @staticmethod
    def _format_profile_value(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, (bool, int, float)):
            return str(value)
        return json.dumps(value, ensure_ascii=False, default=str)
