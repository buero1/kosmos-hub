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
from app.models.site import Site
from app.services.zoho_crm import ZohoCrmService


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


@dataclass(frozen=True)
class CustomerDirectoryDetail:
    entry: CustomerDirectoryEntry
    profile_fields: tuple[CustomerProfileField, ...]
    contacts: tuple["CustomerContactProfile", ...]


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


@dataclass(frozen=True)
class CustomerContactDetail:
    customer: Customer
    contact: CustomerContact
    name: str
    profile_fields: tuple[CustomerProfileField, ...]


class CustomerDirectoryService:
    """Presents CRM customers without ever assigning sites automatically."""

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
            profile_fields = self._profile_fields(customer)
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

    def get_detail(self, *, customer_id: int) -> CustomerDirectoryDetail | None:
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            return None
        linked_by_customer, unlinked_by_domain = self._site_maps()
        profile_fields = self._profile_fields(customer)
        return CustomerDirectoryDetail(
            entry=self._build_entry(customer, linked_by_customer, unlinked_by_domain, profile_fields=profile_fields),
            profile_fields=profile_fields,
            contacts=self._contact_profiles(customer),
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
        profile_fields = self._contact_profile_fields(contact)
        return CustomerContactDetail(
            customer=customer,
            contact=contact,
            name=next((field.value for field in profile_fields if field.label == "Name" and field.value), "Zoho contact"),
            profile_fields=profile_fields,
        )

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

    def _profile_fields(self, customer: Customer) -> tuple[CustomerProfileField, ...]:
        if not customer.encrypted_profile_json:
            return ()
        try:
            profile = json.loads(self.cipher.decrypt(customer.encrypted_profile_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        values = profile.get("fields") if isinstance(profile, dict) else None
        if not isinstance(values, dict):
            return ()
        return tuple(
            CustomerProfileField(label=str(label), value=self._format_profile_value(value))
            for label, value in values.items()
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
                    title=self._format_profile_value(values.get("Position")),
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
        return tuple(
            CustomerProfileField(label=str(label), value=self._format_profile_value(value))
            for label, value in values.items()
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
