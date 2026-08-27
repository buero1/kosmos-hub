"""Safe review and explicit linking of imported CRM customers to Hub sites."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.site import Site
from app.services.zoho_crm import ZohoCrmService


@dataclass(frozen=True)
class CustomerDirectoryEntry:
    customer: Customer
    linked_sites: tuple[Site, ...]
    exact_match_candidate: Site | None


class CustomerDirectoryService:
    """Presents CRM customers without ever assigning sites automatically."""

    def __init__(self, *, db: Session):
        self.db = db

    def list_entries(self, *, query: str = "", candidates_only: bool = False) -> list[CustomerDirectoryEntry]:
        customers = list(self.db.scalars(select(Customer).order_by(Customer.name.asc(), Customer.id.asc())).all())
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

        needle = query.strip().casefold()
        entries: list[CustomerDirectoryEntry] = []
        for customer in customers:
            candidate_sites = unlinked_by_domain.get(customer.website_domain or "", [])
            candidate = candidate_sites[0] if len(candidate_sites) == 1 else None
            linked_sites = tuple(linked_by_customer.get(customer.id, []))
            searchable = " ".join(
                value
                for value in (customer.name, customer.external_id, customer.website_domain, customer.zoho_id)
                if value
            ).casefold()
            if needle and needle not in searchable:
                continue
            if candidates_only and candidate is None:
                continue
            entries.append(
                CustomerDirectoryEntry(
                    customer=customer,
                    linked_sites=linked_sites,
                    exact_match_candidate=candidate,
                )
            )
        return entries

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
