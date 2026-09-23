"""Case operations shared by record pages, mailbox drawers and the agent."""

from functools import partial

from sqlalchemy import select

from app.models.hub_case import HubCase
from app.services.hub_case_field_catalog import HUB_CASE_FIELDS
from app.services.hub_cases import HubCaseEmailSource, HubCaseService
from app.services.hub_operations import (
    HubOperation, HubOperationError, HubOperationInputField, HubOperationResult, register_operation,
)
from app.services.hub_record_access import customer_id, identifier, require_actor


def accessible_source(service, user, access, source_key):
    if not access.can(user, "emails", "view"):
        raise HubOperationError("Die ausgewählte E-Mail ist nicht verfügbar.")
    cases = HubCaseService(db=service.db, cipher=service.cipher)
    linked, mailbox = cases._source_email_record(source_email_key=source_key)
    parents = {"customers": linked.customer_id} if linked is not None else {}
    if mailbox is not None:
        payload = cases._payload(mailbox.encrypted_payload_json)
        parents = {"customers": payload.get("recipient_customer_id"), "leads": payload.get("recipient_lead_id")}
    for module, parent in parents.items():
        if parent and not access.can_access_record(user=user, module_key=module, record_id=int(parent)):
            raise HubOperationError("Die ausgewählte E-Mail ist nicht verfügbar.")
    return cases.source_email(source_email_key=source_key)


def _case(service, user, access, values):
    selected = identifier(values.get("case_id", ""))
    number = values.get("case_number", "").strip()
    if selected is None and not number:
        raise HubOperationError("Bitte die Fall-ID oder eine eindeutige Fall-Nummer angeben.")
    query = select(HubCase).where(HubCase.id == selected) if selected else select(HubCase).where(HubCase.case_number == number)
    matches = [case for case in service.db.scalars(query) if access.can_access_case(user=user, case=case)]
    if len(matches) != 1:
        raise HubOperationError("Der Fall wurde nicht gefunden oder ist nicht eindeutig.")
    return matches[0]


def _defaults(values):
    source = HubCaseEmailSource(key=values["source_email_key"], subject="", customer_id=None) if values.get("source_email_key") else None
    return HubCaseService.new_form_values(source_email=source)


def _execute(service, values, *, action):
    permission = {"create": "create", "update": "edit", "delete": "delete", "link_email": "edit", "unlink_email": "edit"}[action]
    user, access = require_actor(service, "cases", permission)
    cases = HubCaseService(db=service.db, cipher=service.cipher)
    source_key = values.get("source_email_key", "").strip()
    source = accessible_source(service, user, access, source_key) if source_key else None
    submitted = {f"case_field__{field.key}": values[f"case_field__{field.key}"] for field in HUB_CASE_FIELDS
        if not field.read_only and field.key != "customer_name" and f"case_field__{field.key}" in values}
    link_id = ""
    completed_now = False
    if action == "create":
        parent = customer_id(service, user, access, values)
        if source is not None:
            if source.customer_id is not None:
                if ("customer_id" in values or "customer_name" in values) and parent != source.customer_id:
                    raise HubOperationError("Der Kundenbezug der ausgewählten E-Mail darf beim Anlegen nicht geändert werden.")
                parent = source.customer_id
            if cases.linked_case_for_source_email(source_email_key=source.key) is not None:
                raise HubOperationError("Diese E-Mail ist bereits mit einem Fall verknüpft.")
        case = cases.create_case(customer_id=parent, submitted_values=submitted, actor_username=service.actor)
        if source is not None:
            link_id = str(cases.link_email(case_id=case.id, source_email_key=source.key).id)
    else:
        case = _case(service, user, access, values)
        if source is not None and action in {"update", "delete"}:
            linked = cases.linked_case_for_source_email(source_email_key=source.key)
            if linked is None or linked.case.id != case.id:
                raise HubOperationError("Der Fall ist nicht mehr mit dieser E-Mail verknüpft.")
        if action == "update":
            parent = customer_id(service, user, access, values) if "customer_id" in values or "customer_name" in values else case.customer_id
            # A case cannot be moved away from emails belonging to its current customer.
            linked_parents = {link.customer_email.customer_id for link in case.email_links if link.customer_email is not None}
            if linked_parents and linked_parents != {parent}:
                raise HubOperationError("Vor einer Änderung des Kundenbezugs müssen die Kunden-E-Mail-Verknüpfungen gelöst werden.")
            detail = cases.get_detail(case_id=case.id)
            merged = {**{f"case_field__{field.key}": field.form_value for field in detail.fields}, **submitted}
            completed_now = not cases.is_completed_status(detail.status) and cases.is_completed_status(merged.get("case_field__status", ""))
            case = cases.update_case(case_id=case.id, customer_id=parent, submitted_values=merged)
        elif action == "delete":
            cases.delete_case(case_id=case.id)
        elif action == "link_email":
            if source is None:
                raise HubOperationError("Bitte eine E-Mail auswählen.")
            link_id = str(cases.link_email(case_id=case.id, source_email_key=source.key).id)
        elif action == "unlink_email":
            selected_link = identifier(values.get("link_id", ""))
            if selected_link is None:
                raise HubOperationError("Bitte die E-Mail-Verknüpfungs-ID angeben.")
            cases.unlink_email(case_id=case.id, link_id=selected_link)
            link_id = str(selected_link)
    return HubOperationResult(label="Fälle öffnen" if action == "delete" else "Fall öffnen",
        href="/cases" if action == "delete" else f"/cases/{case.id}", record_id=case.id, outputs={
            "case_id": str(case.id), "case_number": cases.case_number(case), "customer_id": str(case.customer_id or ""),
            "link_id": link_id, "completed_now": "true" if completed_now else "false",
        })


def _fields(action):
    fields = () if action == "create" else (
        HubOperationInputField("case_id", "Fall-ID"), HubOperationInputField("case_number", "Fall-Nummer"),
    )
    if action in {"create", "update"}:
        fields += (HubOperationInputField("customer_id", "Kunden-ID"), HubOperationInputField("customer_name", "Kundenname", context_type="customer"))
        fields += tuple(HubOperationInputField(f"case_field__{field.key}", field.label,
            required=field.required if action == "create" else False, options=tuple((value, value) for value in field.options))
            for field in HUB_CASE_FIELDS if not field.read_only and field.key != "customer_name")
    if action in {"create", "update", "delete", "link_email"}:
        fields += (HubOperationInputField("source_email_key", "Ausgewählte E-Mail", required=action == "link_email", context_type="email"),)
    if action == "unlink_email":
        fields += (HubOperationInputField("link_id", "E-Mail-Verknüpfungs-ID", required=True),)
    return fields


_LABELS = {"create": "Fall anlegen", "update": "Fall bearbeiten", "delete": "Fall löschen",
    "link_email": "E-Mail mit Fall verknüpfen", "unlink_email": "E-Mail-Verknüpfung lösen"}


for _action, _label in _LABELS.items():
    register_operation(HubOperation(key=f"cases.{_action}", module="cases", label=_label,
        description=f"{_label}. Änderungen erfolgen nur im Hub, nicht in Zoho. E-Mails werden nicht gelöscht oder versendet.",
        input_guide=("Bestehende Fälle über case_id oder eindeutige case_number auswählen. Kunden über customer_id oder eindeutigen customer_name. "
            "Bei create kann source_email_key eine ausgewählte E-Mail verknüpfen; Kundenbezug und Ursprung E-Mail werden übernommen. "
            "Bei update/delete prüft ein optionaler source_email_key die bestehende Zuordnung. "
            "Bei update bleiben fehlende Felder unverändert; explizit leere optionale Felder werden geleert."),
        preview_fields=(), preview_builder=lambda values, action=_action: (_LABELS[action] + " (nur im Hub)",) + tuple(
            f"{field.label}: {values[field.name] or '(leer)'}" for field in _fields(action) if field.name in values),
        execute=partial(_execute, action=_action), input_fields=partial(_fields, _action),
        defaults=_defaults if _action == "create" else None,
        result_fields=(("case_id", "Fall-ID"), ("case_number", "Fall-Nummer"), ("customer_id", "Kunden-ID, sonst leer"),
            ("link_id", "E-Mail-Verknüpfungs-ID, sonst leer"), ("completed_now", "Gerade abgeschlossen: true/false")),
    ))
