"""Reusable record selection for operations, with no implicit access elevation."""

from sqlalchemy import select

from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_operations import HubOperationError


def identifier(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise HubOperationError("Die Datensatz-ID ist ungültig.")
    return int(value)


def require_actor(service, module: str, action: str):
    user = service.db.scalar(select(HubUser).where(HubUser.username == service.actor, HubUser.is_active.is_(True)))
    access = HubAccessControlService(db=service.db)
    if user is None or not access.can(user, module, "view") or not access.can(user, module, action):
        raise HubOperationError("Für diese Aktion fehlt die Berechtigung.")
    return user, access


def customer_id(service, user, access, values):
    selected = identifier(values.get("customer_id", ""))
    name = values.get("customer_name", "").strip()
    if selected is None and name:
        query = select(Customer.id).where(Customer.name == name)
        allowed = access.accessible_record_ids(user=user, module_key="customers")
        if allowed is not None:
            query = query.where(Customer.id.in_(allowed))
        matches = service.db.scalars(query).all()
        if len(matches) != 1:
            raise HubOperationError("Der Kunde wurde nicht gefunden oder ist nicht eindeutig.")
        selected = matches[0]
    if selected is not None and (
        not access.can_access_record(user=user, module_key="customers", record_id=selected)
        or service.db.get(Customer, selected) is None
    ):
        raise HubOperationError("Der Kunde ist nicht verfügbar.")
    return selected
