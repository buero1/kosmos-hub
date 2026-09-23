"""Shared contact operations for the Hub forms and the agent."""

from functools import partial
from typing import Mapping

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_user import HubUser
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_operations import (
    HubOperation, HubOperationError, HubOperationInputField, HubOperationResult,
    HubOperationService, register_operation,
)
from app.services.zoho_contact_field_catalog import contact_fields
from app.services.zoho_crm import ZohoCrmError, ZohoCrmService


def _identifier(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise HubOperationError("Die Datensatz-ID ist ungültig.")
    return int(value)


def _customer(service, access, user, values):
    identifier = _identifier(values.get("customer_id", ""))
    name = values.get("customer_name", "").strip()
    if identifier is None and name:
        query = select(Customer.id).where(Customer.name == name)
        allowed = access.accessible_record_ids(user=user, module_key="customers")
        if allowed is not None:
            query = query.where(Customer.id.in_(allowed))
        matches = service.db.scalars(query).all()
        if len(matches) != 1:
            raise HubOperationError("Der Kunde wurde nicht gefunden oder ist nicht eindeutig.")
        identifier = matches[0]
    if identifier is not None and (
        not access.can_access_record(user=user, module_key="customers", record_id=identifier)
        or service.db.get(Customer, identifier) is None
    ):
        raise HubOperationError("Der verknüpfte Kunde ist nicht verfügbar.")
    return identifier


def _contact(service, directory, access, user, values):
    identifier = _identifier(values.get("contact_id", ""))
    name = values.get("target_name", "").strip()
    if identifier is None and not name:
        raise HubOperationError("Bitte die Kontakt-ID oder einen eindeutigen Kontaktnamen angeben.")
    query = select(CustomerContact)
    if identifier is not None:
        query = query.where(CustomerContact.id == identifier)
    allowed = access.accessible_record_ids(user=user, module_key="customers")
    if allowed is not None:
        query = query.where(or_(CustomerContact.customer_id.is_(None), CustomerContact.customer_id.in_(allowed)))
    customer_id = _customer(service, access, user, values)
    if customer_id is not None:
        query = query.where(CustomerContact.customer_id == customer_id)
    matches = []
    for contact in service.db.scalars(query):
        if not access.can_access_contact(user=user, contact=contact):
            continue
        if identifier is not None or directory.get_contact_detail_by_id(contact_id=contact.id).name.casefold() == name.casefold():
            matches.append(contact)
    if len(matches) != 1:
        raise HubOperationError("Der Kontakt wurde nicht gefunden oder ist nicht eindeutig.")
    return matches[0]


def _execute(service: HubOperationService, values: Mapping[str, str], *, action: str) -> HubOperationResult:
    user = service.db.scalar(select(HubUser).where(HubUser.username == service.actor, HubUser.is_active.is_(True)))
    access = HubAccessControlService(db=service.db)
    permission = {"create": "create", "update": "edit", "link_customer": "edit", "delete": "delete", "sync": "manage"}[action]
    if user is None or not access.can(user, "contacts", "view") or not access.can(user, "contacts", permission):
        raise HubOperationError("Für diese Kontaktaktion fehlt die Berechtigung.")
    directory = CustomerDirectoryService(db=service.db, cipher=service.cipher)
    submitted = {
        f"contact_field__{field.key}": values[f"contact_field__{field.key}"]
        for field in contact_fields() if f"contact_field__{field.key}" in values
    }
    if action == "create":
        contact = directory.create_hub_contact(
            customer_id=_customer(service, access, user, values), submitted_values=submitted,
        )
    else:
        contact = _contact(service, directory, access, user, values)
        if action == "link_customer":
            if "new_customer_id" not in values:
                raise HubOperationError("Bitte den neuen Kunden angeben; ein leerer Wert entfernt die Verknüpfung.")
            customer_id = _customer(service, access, user, {"customer_id": values["new_customer_id"]})
            contact = directory.set_hub_contact_customer(contact_id=contact.id, customer_id=customer_id)
        elif action == "delete":
            identifier, customer_id = contact.id, contact.customer_id
            directory.delete_contact_from_hub(contact_id=identifier)
            return HubOperationResult(label="Kontakte öffnen", href="/contacts", record_id=identifier,
                outputs={"contact_id": str(identifier), "customer_id": str(customer_id or "")})
        elif action in {"update", "sync"}:
            if action == "update":
                # Both callers submit patches; omitted fields retain the current form values.
                detail = directory.get_contact_detail_by_id(contact_id=contact.id)
                submitted = {**{f"contact_field__{field.key}": field.form_value for field in detail.editable_profile_fields}, **submitted}
            if contact.zoho_id:
                if contact.customer_id is None:
                    raise HubOperationError("Der Zoho-Kontakt hat keine Kundenverknüpfung und kann hier nicht aktualisiert werden.")
                zoho = ZohoCrmService(db=service.db, cipher=service.cipher, public_base_url=get_settings().public_base_url)
                try:
                    if action == "sync":
                        contact = zoho.synchronize_contact(customer_id=contact.customer_id, contact_id=contact.id)
                    else:
                        contact = zoho.update_contact(customer_id=contact.customer_id, contact_id=contact.id, submitted_values=submitted)
                except ZohoCrmError as exc:
                    raise HubOperationError(str(exc)) from exc
            elif action == "sync":
                raise HubOperationError("Dieser Kontakt ist nicht mit Zoho CRM verknüpft.")
            else:
                contact = directory.update_hub_contact(contact_id=contact.id, submitted_values=submitted)
    detail = directory.get_contact_detail_by_id(contact_id=contact.id)
    fields = {field.key: field.form_value for field in detail.editable_profile_fields}
    return HubOperationResult(label="Kontakt öffnen", href=f"/contacts/{contact.id}", record_id=contact.id, outputs={
        "contact_id": str(contact.id), "customer_id": str(contact.customer_id or ""),
        "recipient_name": detail.name, "recipient_email": fields.get("email", ""), "zoho_id": contact.zoho_id or "",
    })


def _input_fields(action: str) -> tuple[HubOperationInputField, ...]:
    fields = tuple(HubOperationInputField(key, label) for key, label in (
        ("customer_id", "Kunden-ID"), ("customer_name", "Kundenname"),
    ))
    if action != "create":
        fields += (HubOperationInputField("contact_id", "Kontakt-ID"), HubOperationInputField("target_name", "Vorhandener Kontaktname"))
    if action in {"create", "update"}:
        fields += tuple(HubOperationInputField(
            f"contact_field__{field.key}", field.label, required=field.required if action == "create" else False,
            options=field.options,
        ) for field in contact_fields(creating=action == "create"))
    if action == "link_customer":
        fields += (HubOperationInputField("new_customer_id", "Neue Kundenverknüpfung"),)
    return fields


_DESCRIPTIONS = {
    "create": ("Kontakt anlegen", "Neuen Kontakt ausschließlich im Hub anlegen, optional mit einem Kunden verknüpft."),
    "update": ("Kontakt bearbeiten", "Kontaktfelder ändern. Hub-Kontakte werden lokal gespeichert; bestehende Zoho-Kontakte werden auch in Zoho CRM geändert."),
    "link_customer": ("Kontakt verknüpfen", "Die Kundenverknüpfung eines Hub-Kontakts setzen oder entfernen. Zoho verwaltet die Verknüpfung seiner Kontakte selbst."),
    "delete": ("Kontakt löschen", "Kontakt nur aus dem Hub löschen. Ein vorhandener Zoho-Datensatz wird nicht gelöscht."),
    "sync": ("Kontakt synchronisieren", "Kontakt aus Zoho CRM neu laden. Dabei werden die Kontaktfelder im Hub durch den aktuellen Zoho-Stand ersetzt."),
}


def _preview(action: str, values: Mapping[str, str]) -> tuple[str, ...]:
    return (_DESCRIPTIONS[action][1],) + tuple(
        f"{field.label}: {values[field.name] or '(leer)'}" for field in _input_fields(action) if field.name in values
    )


for _action, (_label, _description) in _DESCRIPTIONS.items():
    _guide = (
        "Optional customer_id oder eindeutiger customer_name als Kundenverknüpfung; ohne diese Angaben bleibt der neue Kontakt unverknüpft."
        if _action == "create" else
        "Bestehenden Kontakt über contact_id oder eindeutigen target_name wählen. Optional customer_id oder eindeutiger customer_name grenzt die Auswahl ein."
    )
    if _action == "update":
        _guide += " Weggelassene Felder bleiben unverändert, ein expliziter Leerwert leert ein optionales Feld."
    elif _action == "link_customer":
        _guide += " new_customer_id muss übergeben werden: eine Kunden-ID setzt den Bezug, ein Leerwert entfernt ihn."
    register_operation(HubOperation(
        key=f"contacts.{_action}", module="contacts", label=_label, description=_description,
        input_guide=_guide,
        preview_fields=(), preview_builder=partial(_preview, _action),
        execute=partial(_execute, action=_action), input_fields=partial(_input_fields, _action),
        result_fields=(("contact_id", "Kontakt-ID"), ("customer_id", "Verknüpfter Kunde, sonst leer")) + (
            (("recipient_email", "Gespeicherte E-Mail-Adresse, sonst leer"), ("recipient_name", "Kontaktname"), ("zoho_id", "Zoho-ID, sonst leer"))
            if _action != "delete" else ()
        ),
    ))
