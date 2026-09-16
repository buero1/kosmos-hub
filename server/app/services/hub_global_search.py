"""Compact, authenticated type-ahead search across Hub records."""

from __future__ import annotations

from datetime import datetime
import json
import re

from cryptography.fernet import InvalidToken
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerMeetingActivity
from app.models.customer_contact import CustomerContact
from app.models.hub_case import HubCase
from app.models.hub_lead import HubLead
from app.models.site import Site
from app.services.customer_directory import CustomerDirectoryService


MAX_RESULTS_PER_GROUP = 6


class HubGlobalSearchService:
    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def search(self, query: str, *, include_admin_modules: bool) -> list[dict[str, object]]:
        needle = query.strip()[:80]
        if len(needle) < 2:
            return []

        pattern = self._like_pattern(needle)
        phone_forms = self._phone_forms(needle)
        groups: list[dict[str, object]] = []
        self._add_group(groups, "customers", "Kunden", self._customers(pattern, phone_forms, include_sensitive=include_admin_modules))
        if include_admin_modules:
            self._add_group(groups, "contacts", "Kontakte", self._contacts(needle, phone_forms))
            self._add_group(groups, "leads", "Leads", self._leads(needle, phone_forms))
            self._add_group(groups, "cases", "Fälle", self._cases(needle, phone_forms))
        self._add_group(groups, "sites", "Sites", self._sites(pattern))
        self._add_group(groups, "calendar", "Kalender", self._calendar(pattern))
        return groups

    @staticmethod
    def _add_group(groups: list[dict[str, object]], key: str, label: str, items: list[dict[str, str]]) -> None:
        if items:
            groups.append({"key": key, "label": label, "items": items})

    @staticmethod
    def _like_pattern(query: str) -> str:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def _matches(query: str, *values: str) -> bool:
        needle = query.casefold()
        return any(needle in value.casefold() for value in values if value)

    @staticmethod
    def _text(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    def _decrypt_dict(self, encrypted: str) -> dict[str, object]:
        try:
            data = json.loads(self.cipher.decrypt(encrypted))
        except (InvalidToken, TypeError, ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _phone_forms(value: str) -> tuple[str, ...]:
        normalized = re.sub(r"(?:\+49|0049)\s*\(0\)", "+49", value)
        return CustomerDirectoryService._phone_search_forms(normalized)

    @staticmethod
    def _is_phone_field(label: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]", "", label.casefold())
        return normalized.startswith(("telefon", "phone", "mobil", "mobile", "tel")) or normalized.endswith(
            ("telefon", "phone", "mobil", "mobile")
        )

    @staticmethod
    def _matching_phone(
        query_forms: tuple[str, ...], profile: dict[str, object], *, include_sensitive: bool = True,
    ) -> str:
        fields = profile.get("fields")
        if not query_forms or not isinstance(fields, dict):
            return ""
        metadata = profile.get("field_metadata")
        sensitive_labels = {
            definition.get("label")
            for definition in metadata.values()
            if isinstance(definition, dict) and definition.get("sensitive")
        } if isinstance(metadata, dict) and not include_sensitive else set()
        for label, value in fields.items():
            if (
                isinstance(label, str) and isinstance(value, str)
                and label not in sensitive_labels
                and HubGlobalSearchService._is_phone_field(label)
                and any(
                    query_form in value_form
                    for value_form in HubGlobalSearchService._phone_forms(value)
                    for query_form in query_forms
                )
            ):
                return value.strip()
        return ""

    def _customers(
        self, pattern: str, phone_forms: tuple[str, ...], *, include_sensitive: bool,
    ) -> list[dict[str, str]]:
        rows = self.db.execute(
            select(Customer.id, Customer.name, Customer.website_domain)
            .where(
                Customer.is_visible.is_(True),
                or_(Customer.name.ilike(pattern, escape="\\"), Customer.website_domain.ilike(pattern, escape="\\")),
            )
            .order_by(Customer.name.asc(), Customer.id.asc())
            .limit(MAX_RESULTS_PER_GROUP)
        ).all()
        items = [
            {"label": name, "detail": domain or "Kunde", "url": f"/customers/{customer_id}"}
            for customer_id, name, domain in rows
        ]
        if not phone_forms or len(items) >= MAX_RESULTS_PER_GROUP:
            return items

        phone_by_customer: dict[int, str] = {}
        customers = self.db.execute(
            select(Customer.id, Customer.name, Customer.website_domain, Customer.encrypted_profile_json)
            .where(Customer.is_visible.is_(True))
            .order_by(Customer.name.asc(), Customer.id.asc())
        ).all()
        for customer_id, _name, _domain, encrypted in customers:
            if encrypted:
                match = self._matching_phone(
                    phone_forms, self._decrypt_dict(encrypted), include_sensitive=include_sensitive,
                )
                if match:
                    phone_by_customer[customer_id] = match
        for customer_id, encrypted in self.db.execute(
            select(CustomerContact.customer_id, CustomerContact.encrypted_profile_json)
            .join(Customer, Customer.id == CustomerContact.customer_id)
            .where(Customer.is_visible.is_(True))
        ):
            match = self._matching_phone(phone_forms, self._decrypt_dict(encrypted))
            if match and customer_id is not None:
                phone_by_customer.setdefault(customer_id, match)

        existing = {item["url"] for item in items}
        for customer_id, name, _domain, _encrypted in customers:
            url = f"/customers/{customer_id}"
            if customer_id in phone_by_customer and url not in existing:
                items.append({"label": name, "detail": f"Telefon: {phone_by_customer[customer_id]}", "url": url})
                if len(items) >= MAX_RESULTS_PER_GROUP:
                    break
        return items

    def _contacts(self, query: str, phone_forms: tuple[str, ...]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        rows = self.db.execute(
            select(CustomerContact.id, CustomerContact.encrypted_profile_json, Customer.name)
            .outerjoin(Customer, Customer.id == CustomerContact.customer_id)
            .order_by(CustomerContact.id.desc())
        )
        for contact_id, encrypted, customer_name in rows:
            profile = self._decrypt_dict(encrypted)
            fields = profile.get("fields")
            if not isinstance(fields, dict):
                continue
            name = self._text(fields.get("Name")) or " ".join(
                part for part in (self._text(fields.get("Vorname")), self._text(fields.get("Nachname"))) if part
            ) or f"Kontakt {contact_id}"
            email = self._text(fields.get("E-Mail"))
            text_match = self._matches(query, name, email, customer_name or "")
            phone = self._matching_phone(phone_forms, profile)
            if not text_match and not phone:
                continue
            detail = email or customer_name or "Kontakt" if text_match else f"Telefon: {phone}"
            items.append({"label": name, "detail": detail, "url": f"/contacts/{contact_id}"})
            if len(items) >= MAX_RESULTS_PER_GROUP:
                break
        return items

    def _leads(self, query: str, phone_forms: tuple[str, ...]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        rows = self.db.execute(
            select(HubLead.id, HubLead.encrypted_profile_json).order_by(HubLead.id.desc())
        )
        for lead_id, encrypted in rows:
            profile = self._decrypt_dict(encrypted)
            fields = profile.get("fields")
            if not isinstance(fields, dict):
                continue
            company = self._text(fields.get("company"))
            email = self._text(fields.get("email"))
            name = " ".join(
                part for part in (self._text(fields.get("first_name")), self._text(fields.get("last_name"))) if part
            ) or company or f"Lead {lead_id}"
            text_match = self._matches(query, name, company, email)
            phone = self._matching_phone(phone_forms, profile)
            if not text_match and not phone:
                continue
            detail = (company if company != name else email) if text_match else f"Telefon: {phone}"
            items.append({"label": name, "detail": detail, "url": f"/leads/{lead_id}"})
            if len(items) >= MAX_RESULTS_PER_GROUP:
                break
        return items

    def _cases(self, query: str, phone_forms: tuple[str, ...]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        rows = self.db.execute(
            select(HubCase.id, HubCase.case_number, HubCase.encrypted_fields_json, Customer.name)
            .outerjoin(Customer, Customer.id == HubCase.customer_id)
            .order_by(HubCase.id.desc())
        )
        for case_id, case_number, encrypted, customer_name in rows:
            fields = self._decrypt_dict(encrypted)
            description = self._text(fields.get("description"))
            number = case_number or f"Fall {case_id}"
            text_match = self._matches(query, number, customer_name or "", description)
            phone = self._matching_phone(phone_forms, {"fields": fields})
            if not text_match and not phone:
                continue
            items.append({
                "label": number,
                "detail": (customer_name or description[:100] or "Fall") if text_match else f"Telefon: {phone}",
                "url": f"/cases/{case_id}",
            })
            if len(items) >= MAX_RESULTS_PER_GROUP:
                break
        return items

    def _sites(self, pattern: str) -> list[dict[str, str]]:
        rows = self.db.execute(
            select(Site.id, Site.domain, Customer.name)
            .outerjoin(Customer, Customer.id == Site.customer_id)
            .where(Site.domain.ilike(pattern, escape="\\"))
            .order_by(Site.domain.asc(), Site.id.asc())
            .limit(MAX_RESULTS_PER_GROUP)
        ).all()
        return [
            {"label": domain, "detail": customer_name or "Site", "url": f"/sites/{site_id}"}
            for site_id, domain, customer_name in rows
        ]

    def _calendar(self, pattern: str) -> list[dict[str, str]]:
        events: list[tuple[datetime, dict[str, str]]] = []
        for model, kind in ((CustomerCallActivity, "Anruf"), (CustomerMeetingActivity, "Meeting")):
            rows = self.db.execute(
                select(model.name, model.starts_at, Customer.name)
                .outerjoin(Customer, Customer.id == model.customer_id)
                .where(model.name.ilike(pattern, escape="\\"), model.starts_at.is_not(None))
                .order_by(model.starts_at.desc())
                .limit(MAX_RESULTS_PER_GROUP)
            ).all()
            for name, starts_at, customer_name in rows:
                events.append((starts_at, {
                    "label": name,
                    "detail": f"{kind} · {customer_name or starts_at.strftime('%d.%m.%Y')}",
                    "url": f"/calendar?week={starts_at.date().isoformat()}",
                }))
        events.sort(key=lambda event: event[0], reverse=True)
        return [item for _, item in events[:MAX_RESULTS_PER_GROUP]]
