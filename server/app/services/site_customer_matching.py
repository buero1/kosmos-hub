"""Conservative, shared domain matching for Bridge registration and customer saves."""

import json
from urllib.parse import urlsplit

from sqlalchemy import select
from cryptography.fernet import InvalidToken

from app.models.customer import Customer
from app.models.site import Site
from app.services.audit import write_audit_log
from app.services.customer_profile import resolve_customer_fields


def normalized_domain(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        return ""
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value.lstrip("/"))
        if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password:
            return ""
        host = (parsed.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
        _ = parsed.port
    except (ValueError, UnicodeError):
        return ""
    return host[4:] if host.startswith("www.") else host


class SiteCustomerMatchingService:
    def __init__(self, *, db, cipher):
        self.db = db
        self.cipher = cipher

    def customer_domains(self, customer: Customer) -> set[str]:
        profile = {}
        if customer.encrypted_profile_json:
            # Do not guess ownership when a profile cannot be read.
            profile = json.loads(self.cipher.decrypt(customer.encrypted_profile_json))
        fields = {field.key: field.value for field in resolve_customer_fields(profile)}
        values = [fields.get(key) for key in ("website", "work_domain", "work_domain_login")]
        if "website" not in fields:
            values.append(customer.website_domain)
        return {domain for value in values if (domain := normalized_domain(value))}

    def _owners(self) -> dict[str, list[Customer]] | None:
        owners: dict[str, list[Customer]] = {}
        for customer in self.db.scalars(select(Customer)).all():
            try:
                domains = self.customer_domains(customer)
            except (ValueError, TypeError, AttributeError, InvalidToken):
                return None
            for domain in domains:
                owners.setdefault(domain, []).append(customer)
        return owners

    def link(self, site: Site, *, owners=None) -> bool:
        if site.customer_id is not None or site.customer is not None:
            return False
        if site.status != "verified":
            return False
        owners = self._owners() if owners is None else owners
        if owners is None:
            return False
        matches = owners.get(normalized_domain(site.domain), [])
        if len(matches) != 1 or not matches[0].is_visible:
            return False
        site.customer = matches[0]
        site.customer_id = matches[0].id
        write_audit_log(self.db, site=site, actor="kosmos-hub", source="hub",
            action="site-customer-auto-linked", result="ok",
            detail=f"Website {site.domain} matched customer {matches[0].id} by an exact, unique domain.")
        return True

    def customer_saved(self, customer: Customer) -> int:
        self.db.flush()
        domains = self.customer_domains(customer)
        if not domains:
            return 0
        owners = self._owners()
        if owners is None:
            return 0
        sites = self.db.scalars(select(Site).where(
            Site.customer_id.is_(None), Site.status == "verified"
        ).with_for_update()).all()
        return sum(self.link(site, owners=owners) for site in sites
            if normalized_domain(site.domain) in domains)
