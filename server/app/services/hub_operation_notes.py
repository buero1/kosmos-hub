"""Shared customer/lead note CRUD with their existing storage and sync semantics."""

from functools import partial

from app.core.config import get_settings
from app.models.customer_communication import CustomerZohoNote
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_lead_notes import HubLeadNoteService
from app.services.hub_note_catalog import note_fields
from app.services.hub_operations import HubOperation, HubOperationError, HubOperationInputField, HubOperationResult, register_operation
from app.services.hub_record_access import customer_id, identifier, require_actor
from app.services.zoho_crm import ZohoCrmError


def _execute(service, values, *, module, action):
    permission = {"create": "create", "update": "edit", "delete": "delete"}[action]
    user, access = require_actor(service, module, permission)
    parent_key = "customer_id" if module == "customers" else "lead_id"
    parent_id = customer_id(service, user, access, values) if module == "customers" else identifier(values.get(parent_key, ""))
    if parent_id is None or not access.can_access_record(user=user, module_key=module, record_id=parent_id, action="edit"):
        raise HubOperationError("Der zugehörige Datensatz ist nicht zur Bearbeitung verfügbar.")
    if module == "leads" and service.db.get(HubLead, parent_id) is None:
        raise HubOperationError("Der Lead wurde nicht gefunden.")
    notes = CustomerCommunicationService(db=service.db, cipher=service.cipher, public_base_url=get_settings().public_base_url) if module == "customers" else HubLeadNoteService(db=service.db, cipher=service.cipher)
    selected_id = identifier(values.get("note_id", ""))
    submitted = {field.name: values[field.name] for field in note_fields() if field.name in values}
    if action != "create":
        if selected_id is None:
            raise HubOperationError("Bitte die Notiz-ID angeben.")
        record = service.db.get(CustomerZohoNote if module == "customers" else HubLeadNote, selected_id)
        if record is None or getattr(record, parent_key) != parent_id:
            raise HubOperationError("Die Notiz wurde bei diesem Datensatz nicht gefunden.")
        if action == "update":
            current = notes._note_view(record) if module == "customers" else notes._view(record)
            submitted = {**{field.name: getattr(current, field.name) for field in note_fields()}, **submitted}
    args = {parent_key: parent_id}
    if action == "create":
        args.update(actor=service.actor, title=submitted.get("title", ""), content=submitted.get("content", ""))
    else:
        args["note_id"] = selected_id
        if action == "update":
            args.update(submitted)
    try:
        result = getattr(notes, f"{action}_note")(**args)
    except ZohoCrmError as exc:
        raise HubOperationError(str(exc)) from exc
    if module == "customers":
        selected_id = result.note_id
        message, sync_status = result.message, "synced" if result.success else "failed"
    else:
        selected_id = result.id if result is not None else selected_id
        message, sync_status = "Notiz wurde im Hub gespeichert." if action != "delete" else "Notiz wurde gelöscht.", "local"
    return HubOperationResult(label="Notizen öffnen", href=f"/{module}/{parent_id}#{'customer' if module == 'customers' else 'lead'}-notes",
        record_id=selected_id, outputs={"note_id": str(selected_id), parent_key: str(parent_id), "sync_status": sync_status, "message": message})


def _fields(module, action):
    fields = (HubOperationInputField("customer_id" if module == "customers" else "lead_id", "Kunden-ID" if module == "customers" else "Lead-ID", required=module == "leads"),)
    if module == "customers":
        fields += (HubOperationInputField("customer_name", "Eindeutiger Kundenname", context_type="customer"),)
    if action != "create":
        fields += (HubOperationInputField("note_id", "Notiz-ID", required=True),)
    if action != "delete":
        fields += tuple(HubOperationInputField(field.name, field.label, required=field.required if action == "create" else False, max_length=field.maximum)
            for field in note_fields(creating=action == "create"))
    return fields


for _module in ("customers", "leads"):
    for _action, _verb in (("create", "anlegen"), ("update", "bearbeiten"), ("delete", "löschen")):
        _label = ("Kundennotiz" if _module == "customers" else "Lead-Notiz") + " " + _verb
        _storage = "Kundennotizen werden auch in Zoho CRM geändert bzw. gelöscht. Bei fehlgeschlagener Neuanlage in Zoho bleibt die Notiz im Hub gespeichert (sync_status=failed)." if _module == "customers" else "Lead-Notizen werden ausschließlich im Hub geändert oder gelöscht."
        register_operation(HubOperation(key=f"{_module}.notes.{_action}", module=_module, label=_label,
            description=f"{_label}. {_storage}",
            input_guide=("Zugehörigen Datensatz per ID wählen; bei Kunden ist alternativ customer_name möglich. "
                "Neue Notizen benötigen Inhalt; ohne Titel wird dieser aus dem Inhalt abgeleitet. "
                "Bei update nur geänderte Felder übergeben; fehlende Felder bleiben erhalten. Ein explizit leerer Titel oder Inhalt ist beim Bearbeiten ungültig."),
            preview_fields=(), preview_builder=lambda values, module=_module, action=_action, storage=_storage: (storage,) + tuple(
                f"{field.label}: {values[field.name] or '(leer)'}" for field in _fields(module, action) if field.name in values),
            execute=partial(_execute, module=_module, action=_action), input_fields=partial(_fields, _module, _action),
            result_fields=(("note_id", "Notiz-ID"), ("customer_id" if _module == "customers" else "lead_id", "Zugehöriger Datensatz"),
                ("sync_status", "synced, failed (nur lokal gespeichert) oder local"), ("message", "Speicher- oder Synchronisierungshinweis")),
        ))
