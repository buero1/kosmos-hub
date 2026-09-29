"""Purpose-bound SEPA links and transactional, local-only form submissions."""

import json
import re
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from urllib.parse import urlencode

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.core.record_actor import record_actor_scope
from app.core.timezones import BERLIN_TIMEZONE
from app.models.customer import Customer
from app.models.hub_sepa_submission import HubSepaSubmission
from app.models.hub_user import HubUser
from app.services.audit import write_audit_log
from app.services.hub_access_control import HubAccessControlService
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS


TOKEN_FIELD = "ks_hub_sepa_token"
TOKEN_TTL = timedelta(days=14)
FORM_URL = "https://kunden.kosmos-medien.de/sepa-mandat/"
PURPOSE = "hub-sepa-v1"
INVALID_LINK = "Der SEPA-Link ist ungueltig oder abgelaufen. Bitte einen neuen Link anfordern."
FIELD_IDS = {
    TOKEN_FIELD: "token",
    "ks_iban": "iban",
    "ks_bic": "bic",
    "field_a13f37a": "account_holder",
    "ks_kontoinhaber": "account_holder",
    "ks_bank": "bank",
}


class SepaError(ValueError):
    def __init__(self, message, status_code=400, *, code="invalid_request"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def extract_fields(pairs):
    """Read Elementor Advanced Data's stable field IDs, never display labels."""
    result = {}
    for name, value in pairs:
        match = re.fullmatch(r"fields\[([^\[\]]+)\]\[value\]", name)
        field_id = match[1] if match else name
        key = FIELD_IDS.get(field_id)
        if key is None:
            continue
        if key in result or not isinstance(value, str) or len(value) > 2048:
            raise SepaError("Ungueltige oder doppelte Formularfelder.")
        result[key] = value.strip()
    if not result.get("token"):
        raise SepaError("Der persoenliche SEPA-Schluessel fehlt. Bitte den Formularlink aus der E-Mail verwenden.")
    return result


def normalize_bank_fields(fields):
    iban = re.sub(r"\s+", "", fields.get("iban", "")).upper()
    if not iban:
        raise SepaError("Bitte eine gueltige IBAN eingeben.", 422, code="iban_missing")
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban):
        raise SepaError("Bitte eine gueltige IBAN eingeben.", 422, code="iban_format")
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in iban[4:] + iban[:4])
    if iban.startswith("DE") and len(iban) != 22:
        raise SepaError("Die IBAN-Pruefsumme oder Laenge ist ungueltig.", 422, code="iban_length")
    if int(numeric) % 97 != 1:
        raise SepaError("Die IBAN-Pruefsumme oder Laenge ist ungueltig.", 422, code="iban_checksum")
    bic = re.sub(r"\s+", "", fields.get("bic", "")).upper()
    if bic and not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?", bic):
        raise SepaError("Bitte eine gueltige BIC eingeben oder das optionale Feld leer lassen.", 422, code="bic_format")
    holder = " ".join(fields.get("account_holder", "").split())
    bank = " ".join(fields.get("bank", "").split())
    if not holder:
        raise SepaError("Bitte den Kontoinhaber angeben (maximal 255 Zeichen).", 422, code="account_holder_missing")
    if len(holder) > 255 or len(bank) > 255:
        raise SepaError("Bitte den Kontoinhaber angeben (maximal 255 Zeichen).", 422, code="bank_text_length")
    if any(ord(c) < 32 or c in "<>" for c in holder + bank):
        raise SepaError("Kontoinhaber und Bank duerfen nur normalen Text enthalten.", 422, code="bank_text_format")
    return {key: value for key, value in {
        "iban": iban, "bic": bic, "account_holder": holder, "bank": bank,
    }.items() if value}


class HubSepaService:
    def __init__(self, *, db, cipher):
        self.db, self.cipher = db, cipher

    def _permitted(self, user, customer_id):
        access = HubAccessControlService(db=self.db)
        return bool(user and user.is_active and access.can(user, "customers", "view")
                    and access.can_access_record(user=user, module_key="customers", record_id=customer_id, action="edit"))

    def issue_link(self, *, customer, actor, context, now=None):
        user = self.db.scalar(select(HubUser).where(HubUser.username == actor)) if actor else None
        if not self._permitted(user, customer.id):
            raise SepaError("Zum Erstellen eines SEPA-Links fehlt die Bearbeitungsberechtigung fuer diesen Kunden.", 403)
        now = utc(now or datetime.now(UTC))
        # Signed/encrypted claims allow read-only template rendering. Only a token
        # digest is persisted on consumption/revocation, never the bearer token.
        token = self.cipher.encrypt(json.dumps({
            "purpose": PURPOSE, "customer": customer.id, "issuer": user.id,
            "issued": now.isoformat(), "expires": (now + TOKEN_TTL).isoformat(),
            "nonce": token_urlsafe(32),
        }, separators=(",", ":")))
        mapping = {
            "firma": "customername", "vorname": "contactfirstname", "nachname": "contactlastname",
            "adresse": "customerbillingstreet", "plz": "customerbillingpostalcode",
            "ort": "customerbillingcity", "email": "contactemail", "telefon": "customerphone",
            "ks_mandatsreferenz": "customercustomernumber",
        }
        values = {key: context.get(name, "") for key, name in mapping.items()}
        values[TOKEN_FIELD] = token
        return FORM_URL + "?" + urlencode(values), token

    def _claims(self, token, now):
        try:
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError()
            claims = json.loads(self.cipher.decrypt(token))
            issued, expires = utc(datetime.fromisoformat(claims["issued"])), utc(datetime.fromisoformat(claims["expires"]))
            if (claims["purpose"] != PURPOSE or expires - issued != TOKEN_TTL
                    or issued > now or now >= expires
                    or type(claims["customer"]) is not int or claims["customer"] < 1
                    or type(claims["issuer"]) is not int or claims["issuer"] < 1
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", claims["nonce"])):
                raise ValueError()
            return claims, issued, expires
        except (InvalidToken, ValueError, TypeError, KeyError, OverflowError):
            raise SepaError(INVALID_LINK, 403) from None

    def receive(self, fields, *, now=None):
        now = utc(now or datetime.now(UTC))
        token = fields.get("token", "")
        claims, issued, expires = self._claims(token, now)
        values = normalize_bank_fields(fields)
        digest = self.cipher.search_digest(PURPOSE, token)
        payload_digest = self.cipher.search_digest(PURPOSE + "-payload", json.dumps(values, sort_keys=True))
        # Serialize all submissions for a customer, including different links.
        customer = self.db.scalar(select(Customer).where(Customer.id == claims["customer"])
                                  .with_for_update().execution_options(populate_existing=True))
        issuer = self.db.get(HubUser, claims["issuer"])
        if customer is None or not self._permitted(issuer, customer.id):
            raise SepaError(INVALID_LINK, 403)
        receipt = self.db.get(HubSepaSubmission, digest)
        if receipt:
            if receipt.revoked_at:
                raise SepaError(INVALID_LINK, 403)
            if receipt.payload_digest == payload_digest and receipt.received_at:
                return self._success(receipt.received_at, duplicate=True)
            raise SepaError("Dieser SEPA-Link wurde bereits verwendet. Bitte einen neuen Link anfordern.", 409)
        newest = self.db.scalar(select(HubSepaSubmission.received_at).where(
            HubSepaSubmission.customer_id == customer.id, HubSepaSubmission.received_at.is_not(None),
        ).order_by(HubSepaSubmission.received_at.desc()).limit(1))
        if newest and issued <= utc(newest):
            raise SepaError("Es liegt eine neuere SEPA-Uebermittlung vor. Bitte einen neuen Link anfordern.", 409)
        try:
            profile = json.loads(self.cipher.decrypt(customer.encrypted_profile_json))
            if not isinstance(profile.get("fields"), dict):
                raise ValueError()
        except (InvalidToken, ValueError, TypeError, AttributeError):
            raise SepaError("Die Kundendaten konnten nicht aktualisiert werden. Bitte das Kosmos-Team kontaktieren.", 409) from None
        values["sepa_grant_date"] = now.astimezone(BERLIN_TIMEZONE).date().isoformat()
        catalog = {field.key: field for field in ZOHO_ACCOUNT_FIELDS}
        metadata = profile.setdefault("field_metadata", {})
        if not isinstance(metadata, dict):
            raise SepaError("Die Kundenfelder konnten nicht gelesen werden.", 409)
        for key, value in values.items():
            field = catalog[key]
            profile["fields"][field.label] = value
            if key not in metadata:
                metadata[key] = {"label": field.label, "display_type": field.display_type,
                                 "editable": True, "sensitive": field.sensitive}
        with record_actor_scope(self.db, "SEPA-Formular", origin="integration"):
            customer.encrypted_profile_json = self.cipher.encrypt(json.dumps(profile, ensure_ascii=False))
            self.db.add(HubSepaSubmission(token_digest=digest, customer_id=customer.id,
                                        expires_at=expires, received_at=now, payload_digest=payload_digest))
            write_audit_log(self.db, site=None, actor="SEPA-Formular", source="sepa-webhook",
                            action="customer-sepa-received", result="ok",
                            detail=f"Kunde {customer.id}: Bankdaten und Datum SEPA-Erteilung aus dem Formular gespeichert.")
            self.db.flush()
        return self._success(now, duplicate=False)

    def revoke(self, token, *, actor, now=None):
        now = utc(now or datetime.now(UTC))
        claims, _issued, expires = self._claims(token, now)
        customer = self.db.scalar(select(Customer).where(Customer.id == claims["customer"]).with_for_update())
        user = self.db.scalar(select(HubUser).where(HubUser.username == actor))
        if customer is None or not self._permitted(user, customer.id):
            raise SepaError(INVALID_LINK, 403)
        digest = self.cipher.search_digest(PURPOSE, token)
        receipt = self.db.get(HubSepaSubmission, digest)
        if receipt is None:
            receipt = HubSepaSubmission(token_digest=digest, customer_id=customer.id, expires_at=expires)
            self.db.add(receipt)
        receipt.revoked_at = now
        self.db.flush()

    @staticmethod
    def _success(received, *, duplicate):
        return {"success": True, "duplicate": duplicate,
                "sepa_grant_date": utc(received).astimezone(BERLIN_TIMEZONE).date().isoformat()}
