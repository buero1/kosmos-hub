"""Reviewed customer-to-website projection; no arbitrary options or guessed fields."""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.site import Site
from app.services.audit import write_audit_log
from app.services.customer_profile import resolve_customer_fields
from app.services.hub_operations import HubOperationError, HubOperationService
from app.services.hub_record_access import require_actor
from app.services.hub_operation_websites import website_site
from app.services.site_customer_matching import normalized_domain
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


READ_ABILITY = "kosmos-content-kit/read-company-profile"
WRITE_ABILITY = "kosmos-content-kit/write-company-profile"
TARGETS = (("website", "Website URL"), ("work_domain", "Arbeitsdomain URL"), ("work_domain_login", "Arbeitsdomain-Login URL"))
TEXT_IDS = {"field_eb207576fddfa3dc1d976027": "email_break", "field_1f0b7fe9fd0d8290c1d9d4ec": "address",
            "field_dddda53799ea71160555d834": "company_name_address"}
SOURCES = {
    "company_name": ("customer_name", "Kunde-Name"), "phone": ("phone", "Tel."),
    "street": ("billing_street", "Rechnungsadresse - Strasse"),
    "postal_code": ("billing_postal_code", "Rechnungsadresse - PLZ"), "city": ("billing_city", "Rechnungsadresse - Stadt"),
    "country": ("billing_country", "Rechnungsadresse - Land"),
    "email": ("email", "E-Mail"), "email_secondary": ("secondary_email", "Zweite E-Mail-Adresse"),
}
EDITABLE_TYPES = {"text", "textarea", "url", "email", "phone", "phone_link", "email_link", "date", "datetime"}


class ProfileFieldValidationError(HubOperationError):
    """No job was queued; the user can correct values without losing the draft."""


def text(value):
    return value.strip() if isinstance(value, str) else ""


def profile_sources(fields, customer, site):
    result = {key: (text(fields.get(source)), label) for key, (source, label) in SOURCES.items()}
    result["company_name"] = (text(fields.get("customer_name")) or customer.name, "Kunde-Name")
    result["legal_name"] = (result["company_name"][0], "Firmenname")
    result["contact_person"] = (fields["_profile_contact"]["name"], "Erster verknuepfter Kontakt (Reihenfolge der Kontaktliste)")
    result["website"] = (site.home_url, "Zielwebsite")
    city = " ".join(filter(None, [text(fields.get("billing_postal_code")), text(fields.get("billing_city"))]))
    result["postal_code_city"] = (city, "PLZ + Ort")
    address = text(fields.get("customer_address")) or ", ".join(filter(None, [text(fields.get("billing_street")), city]))
    result["address"] = (address, "Kunde-Anschrift / Strasse, PLZ, Ort")
    # Imported formula results may contain a literal 'null' instead of the company name.
    result["company_name_address"] = (", ".join(filter(None, [result["company_name"][0], address])), "Firmenname + Anschrift")
    phone = result["phone"][0]
    result["phone_link"] = ("tel:" + re.sub(r"[^+0-9]", "", phone) if phone else "", "Tel. als Telefon-Link")
    email = result["email"][0]
    result["email_link"] = ("mailto:" + email if email else "", "E-Mail als Email-Link")
    result["email_break"] = (email, "E-Mail als Anzeigetext")
    return result


def customer_context(service, customer_id):
    user, access = require_actor(service, "customers", "edit")
    if type(customer_id) is not int or not access.can_access_record(user=user, module_key="customers", record_id=customer_id, action="edit"):
        raise HubOperationError("Der Kunde ist nicht verfuegbar.")
    customer = service.db.get(Customer, customer_id)
    if customer is None or not customer.is_visible:
        raise HubOperationError("Der Kunde ist nicht verfuegbar.")
    try:
        profile = json.loads(service.cipher.decrypt(customer.encrypted_profile_json)) if customer.encrypted_profile_json else {}
        if not isinstance(profile, dict):
            raise ValueError()
    except (ValueError, TypeError, InvalidToken):
        raise HubOperationError("Die Kundendaten konnten nicht sicher gelesen werden.") from None
    fields = {f.key: f.value for f in resolve_customer_fields(profile) if not f.definition.get("sensitive")}
    if "website" not in fields:
        fields["website"] = customer.website_domain
    from app.services.customer_directory import CustomerDirectoryService
    records = list(service.db.scalars(select(CustomerContact).where(CustomerContact.customer_id == customer.id).order_by(CustomerContact.id)))
    records = [contact for contact in records if access.can_access_contact(user=user, contact=contact)]
    try:
        # Use the same alphabetical order and display names as the customer's contact panel.
        contacts = CustomerDirectoryService(db=service.db, cipher=service.cipher)._contact_profiles_from_records(records)
    except InvalidToken:
        raise HubOperationError("Die verknuepften Kontakte konnten nicht sicher gelesen werden.") from None
    first = contacts[0] if contacts else None
    fields["_profile_contact"] = {"id": first.id if first else None,
        "name": first.name if first and first.name != "Unnamed Zoho contact" else ""}
    fingerprint = hashlib.sha256(json.dumps([customer.name, fields], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return customer, fields, fingerprint


def target_site(service, customer, fields, site_id=None):
    candidates = [(key, label, text(fields.get(key))) for key, label in TARGETS if text(fields.get(key))]
    if not candidates:
        raise HubOperationError("Website URL, Arbeitsdomain URL und Arbeitsdomain-Login URL sind leer.")
    # A nonempty but invalid/unconnected primary URL must never silently redirect a write.
    domain = normalized_domain(candidates[0][2])
    if not domain:
        raise HubOperationError("Die bevorzugte Website-Adresse ist ungueltig. Bitte zuerst im Kunden korrigieren.")
    sites = list(service.db.scalars(select(Site).where(Site.customer_id == customer.id, Site.status == "verified")))
    options = []
    for key, label, url in candidates:
        host = normalized_domain(url)
        matches = [s for s in sites if normalized_domain(s.domain) == host]
        if host and len(matches) == 1 and not any(o["site_id"] == str(matches[0].id) for o in options):
            try:
                website_site(service, matches[0].id, action="edit")
            except HubOperationError:
                continue
            options.append({"site_id": str(matches[0].id), "domain": host, "source": label})
    if site_id is None:
        selected = next((o for o in options if o["domain"] == domain), None)
    else:
        selected = next((o for o in options if o["site_id"] == str(site_id)), None)
    if selected is None:
        raise HubOperationError("Fuer die Zieladresse fehlt eine eindeutige, erlaubte Bridge-Verbindung zu diesem Kunden. Kein automatischer Wechsel auf eine andere Website.")
    site = website_site(service, int(selected["site_id"]), action="edit")
    connections = [c for c in site.connections if c.provider == "kosmos-wordpress" and c.endpoint]
    if len(connections) != 1 or connections[0].status != "active":
        raise HubOperationError("Keine eindeutige Bridge-Verbindung vorhanden.")
    for url in (site.home_url, site.site_url, connections[0].endpoint):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or normalized_domain(url) != selected["domain"] or parsed.username or parsed.password:
            raise HubOperationError("Die Bridge-Adresse passt nicht sicher zur ausgewaehlten Website.")
    return site, options, selected["source"]


def preview(service, customer_id, site_id=None):
    customer, fields, fingerprint = customer_context(service, customer_id)
    site, options, source = target_site(service, customer, fields, site_id)
    try:
        data = SiteMcpProxyService(db=service.db, cipher=service.cipher).execute_ability(site.id, READ_ABILITY, {}, timeout_seconds=15, strict_transport=True)["result"]
    except SiteMcpProxyError as exc:
        if exc.code == "KOSMOS_BRIDGE_ABILITY_NOT_FOUND":
            raise HubOperationError("Bitte Kosmos Content Kit auf 0.3.3 oder neuer aktualisieren und aktivieren. Die Uebertragung benoetigt die WordPress Abilities API (WordPress 6.9+).") from None
        raise HubOperationError("Das Firmenprofil konnte nicht gelesen werden. Bridge-Verbindung pruefen; es wurde nichts gesendet.") from None
    except (OSError, KeyError, TypeError, ValueError):
        raise HubOperationError("Das Firmenprofil konnte nicht sicher gelesen werden.") from None
    try:
        schema, current, revision = data["schema"]["fields"], data["values"], data["revision"]
        if not isinstance(schema, dict) or not isinstance(current, dict) or len(schema) > 300 or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError()
        sources = profile_sources(fields, customer, site)
        rows, writable, constraints = [], {}, {}
        for key, definition in schema.items():
            label, kind = definition["label"], definition["type"]
            if not isinstance(key, str) or not 1 <= len(key) <= 128 or not isinstance(label, str) or len(label) > 250:
                raise ValueError()
            value, origin = sources.get(TEXT_IDS.get(key, key), ("", "Keine eindeutige Kundenangabe"))
            old = current.get(key, "")
            if not isinstance(old, (str, int)) or len(str(old)) > 10000:
                raise ValueError()
            limit = min(int(definition.get("max_length", 500)), 10000)
            editable = kind in EDITABLE_TYPES and limit > 0
            allowed = False
            if editable:
                constraints[key] = {"type": kind, "max_length": limit, "label": label}
                try:
                    value = validate_field(value, constraints[key])
                    writable[key], allowed = value, True
                except ProfileFieldValidationError:
                    pass
            rows.append({"id": key, "label": label, "type": kind, "source": origin,
                "current": old, "proposed": value if allowed else "", "selectable": allowed and value != old,
                "editable": editable, "max_length": limit,
                "reason": "Unveraendert" if allowed and value == old else "" if allowed else
                    "Keine passende Kundenangabe; kann manuell eingetragen werden" if editable else "Nicht uebertragbar; bleibt unveraendert"})
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HubOperationError("Das Firmenprofil hat eine ungueltige Antwort geliefert. Es wurde nichts gesendet.") from None
    token = service.cipher.encrypt(json.dumps({"purpose": "customer-website-profile-v2", "actor": service.actor,
        "expires": (datetime.now(UTC) + timedelta(minutes=30)).timestamp(), "customer_id": customer_id,
        "site_id": site.id, "uuid": site.uuid, "domain": normalized_domain(site.domain), "fingerprint": fingerprint,
        "revision": revision, "values": writable, "fields": constraints}))
    return {"customer_id": str(customer_id), "site_id": str(site.id), "domain": site.domain, "target_source": source,
        "options": options, "rows": rows, "preview_token": token}


def validate_field(raw, definition):
    label, kind = definition["label"], definition["type"]
    def invalid(reason):
        raise ProfileFieldValidationError(f"{label}: {reason}")
    if not isinstance(raw, str) or not raw.strip():
        invalid("Bitte einen Wert eingeben oder das Feld abwaehlen. Leere Werte werden nicht uebertragen.")
    value = raw.strip().replace("\r\n", "\n").replace("\r", "\n")
    if len(value) > definition["max_length"]:
        invalid(f"Maximal {definition['max_length']} Zeichen erlaubt.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|<[^>]*>", value):
        invalid("Bitte nur Text ohne HTML oder Steuerzeichen eingeben.")
    if kind != "textarea":
        value = re.sub(r"[\r\n\t ]+", " ", value)
    if kind in {"email", "email_link"}:
        email = re.sub(r"^mailto:", "", value, flags=re.I) if kind == "email_link" else value
        if not re.fullmatch(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?", email):
            invalid("Bitte eine gueltige E-Mail-Adresse eingeben.")
    elif kind == "url":
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or re.search(r"\s|\\", value):
                raise ValueError()
            parsed.port
        except ValueError:
            invalid("Bitte eine Adresse mit https:// oder http:// ohne Zugangsdaten eingeben.")
    elif kind in {"phone", "phone_link"}:
        number = re.sub(r"^tel:", "", value, flags=re.I) if kind == "phone_link" else value
        if not re.fullmatch(r"\+?[0-9 ()/.\-]+", number) or not re.search(r"[0-9]", number):
            invalid("Bitte eine Telefonnummer eingeben.")
    elif kind in {"date", "datetime"}:
        pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}" + (r"T[0-9]{2}:[0-9]{2}" if kind == "datetime" else "")
        try:
            if not re.fullmatch(pattern, value):
                raise ValueError()
            datetime.strptime(value, "%Y-%m-%dT%H:%M" if kind == "datetime" else "%Y-%m-%d")
        except ValueError:
            invalid("Bitte ein gueltiges Datum" + (" mit Uhrzeit" if kind == "datetime" else "") + " eingeben.")
    return value


def prepare(service, *, customer_id, site_id, preview_token, field_ids, edited_values_json="{}"):
    customer, fields, fingerprint = customer_context(service, customer_id)
    site, _options, _source = target_site(service, customer, fields, site_id)
    try:
        if not isinstance(preview_token, str) or len(preview_token) > 120000:
            raise ValueError()
        proof = json.loads(service.cipher.decrypt(preview_token))
        if (proof["purpose"] != "customer-website-profile-v2" or proof["actor"] != service.actor or
                proof["expires"] < datetime.now(UTC).timestamp() or proof["customer_id"] != customer_id or
                proof["site_id"] != site_id or proof["uuid"] != site.uuid or proof["domain"] != normalized_domain(site.domain) or
                proof["fingerprint"] != fingerprint):
            raise ValueError()
        if (not isinstance(field_ids, list) or not 1 <= len(field_ids) <= 100 or
                any(not isinstance(key, str) or key not in proof["fields"] for key in field_ids) or len(set(field_ids)) != len(field_ids)):
            raise ValueError()
    except (ValueError, KeyError, TypeError, InvalidToken):
        raise HubOperationError("Vorschau ungueltig, abgelaufen oder Kundendaten geaendert. Bitte neu laden und mindestens ein Feld auswaehlen.") from None
    try:
        if not isinstance(edited_values_json, str) or len(edited_values_json) > 120000:
            raise ValueError()
        edited = json.loads(edited_values_json)
        if not isinstance(edited, dict) or set(edited) - set(field_ids):
            raise ValueError()
    except (ValueError, TypeError):
        raise ProfileFieldValidationError("Nur Werte fuer ausgewaehlte Vorschau-Felder sind erlaubt.") from None
    values = {key: validate_field(edited.get(key, proof["values"].get(key, "")), proof["fields"][key]) for key in field_ids}
    # Match the plugin's encoded payload limit, including Unicode and JSON overhead.
    if len(json.dumps({"values": values, "revision": proof["revision"]}, ensure_ascii=True).replace("/", "\\/")) > 131072:
        raise ProfileFieldValidationError("Die ausgewaehlten Texte sind zusammen zu lang. Bitte weniger Felder uebertragen.")
    return site, values, proof["revision"]


@dataclass
class ProfileSendResult:
    data: dict


class CustomerWebsiteProfileService:
    def __init__(self, *, db, cipher):
        self.db, self.cipher = db, cipher

    def send(self, *, customer_id: int, site_id: int, preview_token: str, field_ids: list[str], actor: str, edited_values_json: str = "{}"):
        service = HubOperationService(db=self.db, cipher=self.cipher, actor=actor)
        site, values, revision = prepare(service, customer_id=customer_id, site_id=site_id, preview_token=preview_token,
            field_ids=field_ids, edited_values_json=edited_values_json)
        status, message = "uncertain", "Uebertragung nicht sicher bestaetigt. Firmenprofil pruefen; nicht ungeprueft erneut senden."
        try:
            data = SiteMcpProxyService(db=self.db, cipher=self.cipher).execute_ability(site_id, WRITE_ABILITY,
                {"values": values, "revision": revision}, timeout_seconds=20, strict_transport=True)["result"]
            if data.get("values") == values and re.fullmatch(r"[a-f0-9]{64}", data.get("revision", "")):
                status, message = "succeeded", f"{len(values)} Felder erfolgreich ins Firmenprofil uebertragen."
        except SiteMcpProxyError as exc:
            if exc.code == "CONFLICT":
                status, message = "failed", "Das Firmenprofil wurde inzwischen geaendert. Nichts ueberschrieben; neue Vorschau laden."
            elif exc.status_code in {401, 403, 404, 422}:
                status, message = "failed", "Die Website hat die Uebertragung abgewiesen. Berechtigung, Plugin-Version und Feldwerte pruefen."
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        write_audit_log(self.db, site=site, actor=actor, source="hub", action="customer-company-profile-send",
            result=status, detail=f"customer={customer_id}; fields={','.join(field_ids)}; {message}")
        return ProfileSendResult({"customer_id": customer_id, "site_id": site_id, "domain": site.domain,
            "status": status, "message": message, "field_count": len(values)})
