"""Explicit human-only access to encrypted customer IBANs."""
import json

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.services.audit import write_audit_log
from app.services.customer_profile import resolve_customer_fields
from app.services.hub_access_control import HubAccessControlService


class IbanRevealError(ValueError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def reveal_customer_iban(*, db, cipher, user_id, session_version, customer_id):
    user = db.scalar(select(HubUser).where(HubUser.id == user_id).execution_options(populate_existing=True))
    # `admin` is the immutable Superadmin role, not a configurable module grant.
    if (user is None or not user.is_active or user.role != "admin"
            or type(session_version) is not int or user.session_version != session_version):
        raise IbanRevealError("Nur Superadmins duerfen die IBAN anzeigen.", 403)
    access = HubAccessControlService(db=db)
    if not access.can_access_record(user=user, module_key="customers", record_id=customer_id, action="view"):
        raise IbanRevealError("Der Kunde ist nicht verfuegbar.", 404)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise IbanRevealError("Der Kunde ist nicht verfuegbar.", 404)
    try:
        profile = json.loads(cipher.decrypt(customer.encrypted_profile_json))
        if not isinstance(profile, dict) or not isinstance(profile.get("fields"), dict):
            raise ValueError()
        value = next((field.value for field in resolve_customer_fields(profile) if field.key == "iban"), None)
        if value is not None and (not isinstance(value, str) or len(value) > 64):
            raise ValueError()
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise IbanRevealError("Die gespeicherte IBAN konnte nicht gelesen werden.", 409) from None
    if not value or not value.strip():
        raise IbanRevealError("Bei diesem Kunden ist keine IBAN hinterlegt.", 404)
    write_audit_log(db, site=None, actor=user.username, source="hub-web",
                    action="customer-iban-revealed", result="ok",
                    detail=f"Customer {customer_id}: IBAN angezeigt; Wert nicht protokolliert.")
    return value.strip()
