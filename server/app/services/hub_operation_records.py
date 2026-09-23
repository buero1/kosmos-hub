"""Customer and Lead operations used by forms and the Hub Agent alike."""

import json
from collections.abc import Mapping

from app.core.config import get_settings
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_customer_field_catalog import customer_create_defaults, customer_create_fields
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS, HUB_LEAD_SUBFORMS
from app.services.hub_leads import HubLeadService
from app.services.hub_deletion import LEAD_DELETE_NOTICE
from app.services.hub_operations import (
    HubOperation, HubOperationError, HubOperationInputField as Input, HubOperationResult,
    register_operation,
)
from app.services.hub_record_access import identifier, require_actor
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS
from app.services.zoho_crm import ZohoCrmService


def form_input(values: Mapping[str, object], *, kind: str) -> dict[str, str]:
    """Transport adapter only: the shared operation owns validation and changes."""
    root = f"{kind}_field__"
    subform = f"{kind}_subform__"
    result = {key[len(root):]: json.dumps(value, ensure_ascii=False) if isinstance(value, (tuple, list)) else str(value)
        for key, value in values.items() if key.startswith(root)}
    rows = {key: value for key, value in values.items() if key.startswith(subform)}
    if rows:
        result["subform_values"] = json.dumps(rows, ensure_ascii=False)
    return result


def _subforms(values, *, prefix, allowed):
    try:
        rows = json.loads(values.get("subform_values", "{}"))
    except (TypeError, ValueError) as exc:
        raise HubOperationError("Die Unterformular-Eingaben sind ungueltig.") from exc
    if not isinstance(rows, dict) or len(rows) > 1000:
        raise HubOperationError("Die Unterformular-Eingaben sind ungueltig.")
    for key, value in rows.items():
        if key not in allowed or not key.startswith(prefix) or not isinstance(value, str) or len(value) > 20_000:
            raise HubOperationError("Unbekanntes oder ungueltiges Unterformular-Feld.")
    return rows


def _check_keys(values, allowed):
    if set(values) - set(allowed):
        raise HubOperationError("Die Eingabe enthaelt unbekannte oder nicht bearbeitbare Felder.")
    if any(not isinstance(value, str) for value in values.values()):
        raise HubOperationError("Die Feldeingaben muessen Textwerte sein.")


def _selected(service, module, values, action):
    user, access = require_actor(service, module, action)
    name = "customer_id" if module == "customers" else "lead_id"
    selected = identifier(values.get(name, ""))
    if selected is None or not access.can_access_record(user=user, module_key=module, record_id=selected, action=action):
        raise HubOperationError("Der Datensatz ist nicht verfuegbar.")
    return user, access, selected


def customer_detail(service, values, *, action="view"):
    user, access, selected = _selected(service, "customers", values, action)
    detail = CustomerDirectoryService(db=service.db, cipher=service.cipher).get_detail(
        customer_id=selected,
        include_sensitive=access.can_access_record(user=user, module_key="customers", record_id=selected, action="edit"),
    )
    if detail is None:
        raise HubOperationError("Der Datensatz ist nicht verfuegbar.")
    return detail


def customer_entries(service, **filters):
    user, access = require_actor(service, "customers", "view")
    if filters.get("unread_email_only") and not access.can(user, "emails", "view"):
        raise HubOperationError("Fuer den E-Mail-Filter fehlt die Berechtigung.")
    allowed = access.accessible_record_ids(user=user, module_key="customers")
    editable = None if allowed is None else {record_id for record_id in allowed
        if access.can_access_record(user=user, module_key="customers", record_id=record_id, action="edit")}
    return CustomerDirectoryService(db=service.db, cipher=service.cipher).list_entries(
        **filters, allowed_customer_ids=allowed, include_sensitive=access.can(user, "customers", "edit"),
        sensitive_customer_ids=editable, include_contact_fields=access.can(user, "contacts", "view"),
    )


def lead_entries(service):
    user, access = require_actor(service, "leads", "view")
    return HubLeadService(db=service.db, cipher=service.cipher).list_leads(
        allowed_lead_ids=access.accessible_record_ids(user=user, module_key="leads"))


def customer_suggestions(service, *, query="", status="all", industry="all", email="all"):
    from app.services.zoho_crm import ZOHO_RELEVANT_ACCOUNT_STATUSES
    require_actor(service, "customers", "view")
    if status not in {*ZOHO_RELEVANT_ACCOUNT_STATUSES, "all"} or email not in {"all", "unread"}:
        raise HubOperationError("Ungueltiger Kundenfilter.")
    if len(query.strip()) < 2:
        return {"suggestions": []}
    entries = customer_entries(service, query=query.strip(), status=None if status == "all" else status,
        industry=None if industry == "all" else industry, unread_email_only=email == "unread")
    return {"suggestions": [{"id": entry.customer.id, "name": entry.customer.name,
        "website": entry.customer.website_domain or "", "status": entry.account_status or ""}
        for entry in entries[:7]]}


def lead_detail(service, values, *, action="view"):
    _user, _access, selected = _selected(service, "leads", values, action)
    detail = HubLeadService(db=service.db, cipher=service.cipher).get_detail(lead_id=selected)
    if detail is None:
        raise HubOperationError("Der Datensatz ist nicht verfuegbar.")
    return detail


def _customer_create(service, values):
    user, access = require_actor(service, "customers", "create")
    _check_keys(values, (f.key for f in customer_create_fields()))
    customer = CustomerDirectoryService(db=service.db, cipher=service.cipher).create_hub_customer(
        submitted_values={f"customer_field__{key}": value for key, value in values.items()},
    )
    access.assign_created_record(user=user, module_key="customers", record_id=customer.id)
    return _customer_result(customer)


def _customer_result(customer):
    return HubOperationResult(label=customer.name, href=f"/customers/{customer.id}", record_id=customer.id,
        outputs={"customer_id": str(customer.id), "customer_name": customer.name})


def customer_subform_inputs(detail):
    return {f"customer_subform__{subform.key}__{index}__{field.key}"
        for subform in detail.subforms for index in (*range(len(subform.records)), "new")
        for field in subform.fields if field.editable} | {
        f"customer_subform__{subform.key}__{index}__delete"
        for subform in detail.subforms for index in range(len(subform.records))}


def _customer_update(service, values):
    detail = customer_detail(service, values, action="edit")
    editable = {field.key: field for field in detail.editable_profile_fields}
    _check_keys(values, {*editable, "customer_id", "subform_values"})
    submitted = {f"customer_field__{key}": value for key, value in values.items() if key in editable}
    submitted.update(_subforms(values, prefix="customer_subform__", allowed=customer_subform_inputs(detail)))
    customer = detail.entry.customer
    if customer.zoho_id:
        # The HTML form omits unchecked boxes. Partial operations must not clear omitted boxes.
        for key, field in editable.items():
            if field.display_type == "Boolesch":
                submitted.setdefault(f"customer_field__{key}", field.form_value)
        customer = ZohoCrmService(db=service.db, cipher=service.cipher, public_base_url=get_settings().public_base_url).update_customer_fields(
            customer_id=customer.id, submitted_values=submitted,
        )
    else:
        defaults = {f"customer_field__{key}": field.form_value for key, field in editable.items()}
        customer = CustomerDirectoryService(db=service.db, cipher=service.cipher).update_hub_customer(
            customer_id=customer.id, submitted_values={**defaults, **submitted},
        )
    return _customer_result(customer)


def lead_subform_inputs(detail=None):
    counts = {subform.key: len(subform.rows) for subform in detail.subforms} if detail else {}
    return {f"lead_subform__{subform.key}__{index}__{field.key}"
        for subform in HUB_LEAD_SUBFORMS for index in (*range(counts.get(subform.key, 0)), "new")
        for field in subform.fields if not field.read_only} | {
        f"lead_subform__{subform.key}__{index}__delete"
        for subform in HUB_LEAD_SUBFORMS for index in range(counts.get(subform.key, 0))}


def _lead_values(values, detail=None):
    fields = {field.key: field for field in HUB_LEAD_FIELDS if not field.read_only}
    _check_keys(values, {*fields, "lead_id", "subform_values"})
    submitted = _subforms(values, prefix="lead_subform__", allowed=lead_subform_inputs(detail))
    for key, value in values.items():
        if key not in fields:
            continue
        if fields[key].display_type == "Mehrfachauswahl":
            try:
                value = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise HubOperationError("Mehrfachauswahl muss als JSON-Liste angegeben werden.") from exc
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise HubOperationError("Die Mehrfachauswahl ist ungueltig.")
        elif fields[key].display_type == "Boolesch" and value.casefold() not in {"", "true", "false", "on", "off", "1", "0", "yes", "no"}:
            raise HubOperationError("Die Ja/Nein-Auswahl ist ungueltig.")
        submitted[f"lead_field__{key}"] = value
    return submitted


def _lead_create(service, values):
    user, access = require_actor(service, "leads", "create")
    lead = HubLeadService(db=service.db, cipher=service.cipher).create_lead(submitted_values=_lead_values(values))
    access.assign_created_record(user=user, module_key="leads", record_id=lead.id)
    return _lead_result(service, lead.id)


def _lead_update(service, values):
    detail = lead_detail(service, values, action="edit")
    HubLeadService(db=service.db, cipher=service.cipher).update_lead(lead_id=detail.lead.id, submitted_values=_lead_values(values, detail))
    return _lead_result(service, detail.lead.id)


def _lead_result(service, lead_id):
    detail = HubLeadService(db=service.db, cipher=service.cipher).get_detail(lead_id=lead_id)
    email = next((field.form_value for field in detail.fields if field.key == "email"), "")
    return HubOperationResult(label=detail.name, href=f"/leads/{lead_id}", record_id=lead_id,
        outputs={"lead_id": str(lead_id), "lead_name": detail.name, "recipient_email": str(email or "")})


def _lead_delete(service, values):
    _check_keys(values, {"lead_id"})
    detail = lead_detail(service, values, action="delete")
    HubLeadService(db=service.db, cipher=service.cipher).delete_lead(lead_id=detail.lead.id)
    return HubOperationResult(label=detail.name, href="/leads", record_id=detail.lead.id)


SUBFORMS_INPUT = Input("subform_values", "Unterformular-Eingaben aus dem Leseergebnis", max_length=100_000,
    encoding="JSON object encoded as string: form input name -> text value. Only supplied fields change.")


def _customer_inputs(creating):
    if creating:
        return tuple(Input(f.key, f.label, required=f.required, options=f.options, max_length=f.max_length) for f in customer_create_fields())
    return (Input("customer_id", "Kunden-ID", required=True, context_type="customer"), *(
        Input(f.key, f.label) for f in ZOHO_ACCOUNT_FIELDS if f.key != "record_id" and not f.subform_parent), SUBFORMS_INPUT)


def _lead_inputs(creating):
    identity = () if creating else (Input("lead_id", "Lead-ID", required=True, context_type="lead"),)
    return (*identity, *(Input(f.key, f"{f.label} ({f.display_type})", required=f.required if creating else False,
        options=f.options, max_length=HubLeadService._MAX_FIELD_LENGTH,
        encoding="JSON array encoded as string" if f.display_type == "Mehrfachauswahl" else "")
        for f in HUB_LEAD_FIELDS if not f.read_only), SUBFORMS_INPUT)


for module, label, create, update, inputs, defaults in (
    ("customers", "Kunde", _customer_create, _customer_update, _customer_inputs,
     lambda _: {key.removeprefix("customer_field__"): value for key, value in customer_create_defaults().items()}),
    ("leads", "Lead", _lead_create, _lead_update, _lead_inputs,
     lambda _: {key.removeprefix("lead_field__"): str(value) for key, value in HubLeadService.new_form_values().items()}),
):
    for action, verb, handler in (("create", "anlegen", create), ("update", "bearbeiten", update)):
        register_operation(HubOperation(
            key=f"{module}.{action}", module=module, label=f"{label} {verb}",
            description=f"{label} {verb} mit derselben Validierung und denselben Workflows wie die Maske. "
                + ("Neue Datensaetze werden lokal im Hub angelegt." if action == "create" else "Nur angegebene Felder aendern; ausgelassene Felder bleiben erhalten. Vorher den Datensatz lesen; dessen edit_fields und Optionen sind verbindlich. Zoho-verknuepfte Kunden werden wie in der Maske in Zoho gespeichert."),
            input_guide="Datumswerte ISO YYYY-MM-DD, DatumZeit YYYY-MM-DDTHH:MM (Europe/Berlin), Boolesch true/false. Unterformular-Eingaben optional aus dem Leseergebnis.",
            preview_fields=(("customer_id", "Kunden-ID"), ("lead_id", "Lead-ID")),
            preview_builder=lambda values: tuple(f"{key}: {value}" for key, value in values.items()),
            execute=handler, defaults=defaults if action == "create" else None,
            input_fields=lambda inputs=inputs, creating=action == "create": inputs(creating),
            result_fields=(("customer_id", "Kunden-ID"), ("customer_name", "Kundenname")) if module == "customers" else
                (("lead_id", "Lead-ID"), ("lead_name", "Lead-Name"), ("recipient_email", "Lead-E-Mail")),
        ))

register_operation(HubOperation(
    key="leads.delete", module="leads", label="Lead loeschen", description="Lead nur im Hub loeschen, nicht in Zoho.",
    input_guide="lead_id; vorher records.deletion_preview mit /leads/{lead_id}/delete pruefen.",
    preview_fields=(("lead_id", "Lead-ID"),),
    preview_builder=lambda values: (f"Lead-ID: {values.get('lead_id', '')}", *LEAD_DELETE_NOTICE), execute=_lead_delete,
    input_fields=lambda: (Input("lead_id", "Lead-ID", required=True, context_type="lead"),),
))
