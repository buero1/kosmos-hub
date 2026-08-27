"""Safe review and explicit linking of imported CRM customers to Hub sites."""

from __future__ import annotations

from dataclasses import dataclass
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
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


class CustomerDirectoryService:
    """Presents CRM customers without ever assigning sites automatically."""

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_entries(self, *, query: str = "", status: str | None = None) -> list[CustomerDirectoryEntry]:
        customers = list(self.db.scalars(select(Customer).order_by(Customer.name.asc(), Customer.id.asc())).all())
        linked_by_customer, unlinked_by_domain = self._site_maps()

        needle = query.strip().casefold()
        entries: list[CustomerDirectoryEntry] = []
        for customer in customers:
            profile_fields = self._profile_fields(customer)
            entry = self._build_entry(customer, linked_by_customer, unlinked_by_domain, profile_fields=profile_fields)
            if status is not None and (entry.account_status or "").casefold() != status.casefold():
                continue
            searchable_values = [customer.name, customer.external_id, customer.website_domain, customer.zoho_id]
            searchable_values.extend(f"{field.label} {field.value or ''}" for field in profile_fields)
            searchable = " ".join(
                value
                for value in searchable_values
                if value
            ).casefold()
            if needle and needle not in searchable:
                continue
            entries.append(entry)
        return entries

    def get_detail(self, *, customer_id: int) -> CustomerDirectoryDetail | None:
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            return None
        linked_by_customer, unlinked_by_domain = self._site_maps()
        profile_fields = self._profile_fields(customer)
        return CustomerDirectoryDetail(
            entry=self._build_entry(customer, linked_by_customer, unlinked_by_domain, profile_fields=profile_fields),
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
    def _format_profile_value(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, (bool, int, float)):
            return str(value)
        return json.dumps(value, ensure_ascii=False, default=str)
