"""Paginated CRM reads; no completion, synchronization or read-status writes."""

from functools import partial
from datetime import datetime
from app.core.timezones import iso_berlin_time

from app.services.hub_crm_readers import HubCrmReadService
from app.services.hub_activity_catalog import activity_fields
from app.services.hub_operation_activities import _existing_values
from app.services.hub_operations import HubOperationInputField as Field, HubQuery, register_query
from app.services.hub_record_access import identifier, require_actor
from app.services.hub_activity_responsibility import ActivityResponsibility, ACTIVITY_VIEWS
from app.services.hub_operation_queries import _fields, _offset


def reader(service):
    return HubCrmReadService(db=service.db, cipher=service.cipher, actor=service.actor)


def page(items, values):
    offset = _offset(values, "offset")
    return {"items": items[offset:offset + 25], "total": len(items), "next_offset": str(offset + 25) if len(items) > offset + 25 else ""}


def list_records(service, values, *, kind):
    domain = reader(service)
    query = values.get("query", "").casefold()
    customer = identifier(values.get("customer_id", ""))
    lead = identifier(values.get("lead_id", ""))
    status = values.get("status", "")
    items = []
    if kind == "contacts":
        for entry in domain.contact_entries():
            if customer and (entry.customer is None or entry.customer.id != customer):
                continue
            if query and query not in " ".join(entry.contact.searchable_values).casefold():
                continue
            items.append({"contact_id": str(entry.contact.id), "name": entry.contact.name, "email": entry.contact.email or "",
                          "customer_id": str(entry.customer.id) if entry.customer else "", "url": f"/contacts/{entry.contact.id}"})
    elif kind == "cases":
        for entry in domain.case_entries(query=query):
            if customer and entry.case.customer_id != customer or status and entry.status != status:
                continue
            items.append({"case_id": str(entry.case.id), "case_number": entry.case_number, "status": entry.status,
                          "customer_id": str(entry.case.customer_id or ""), "customer_name": entry.customer_name, "url": f"/cases/{entry.case.id}"})
    else:
        for entry in domain.activity_entries(kind, view=values.get("view") or "all"):
            row = domain.activity_record(kind, entry.id)
            if customer and row.customer_id != customer or lead and row.lead_id != lead or status and row.status != status:
                continue
            if query and query not in f"{entry.name} {entry.description} {entry.related_name}".casefold():
                continue
            items.append({"activity_id": str(entry.id), "kind": kind, "name": entry.name, "status": entry.status,
                          "assignee_user_id": str(entry.assignee_user_id or ""), "assignee_name": entry.assignee_name,
                          "can_edit": entry.can_edit, "can_delete": entry.can_delete,
                          "scheduled_at": iso_berlin_time(entry.scheduled_at) if entry.scheduled_at else "",
                          "customer_id": str(row.customer_id or ""), "lead_id": str(row.lead_id or ""), "url": entry.activity_href})
    return page(items, values)


def read_record(service, values, *, kind):
    domain = reader(service)
    identity = {"contacts": "contact_id", "cases": "case_id"}.get(kind, "activity_id")
    record_id = identifier(values[identity])
    if kind == "contacts":
        detail = domain.contact_detail(record_id)
        return {identity: str(record_id), "name": detail.name, "customer_id": str(detail.contact.customer_id or ""),
                "fields": _fields(detail.profile_fields, values),
                "edit_fields": {f"contact_field__{f.key}": {"label": f.label, "required": f.required, "options": dict(f.options)} for f in detail.editable_profile_fields},
                "url": f"/contacts/{record_id}"}
    if kind == "cases":
        detail = domain.case_detail(record_id, customer_id=identifier(values.get("customer_id", "")))
        start = _offset(values, "link_offset")
        return {identity: str(record_id), "case_number": detail.case_number, "customer_id": str(detail.case.customer_id or ""),
                "fields": _fields(detail.fields, values),
                "edit_fields": {f"case_field__{f.key}": {"label": f.label, "required": f.required, "options": f.options} for f in detail.fields if not f.read_only},
                "email_links": [{"link_id": str(link.link_id), "email_key": link.source_key, "subject": link.subject} for link in detail.linked_emails[start:start + 25]],
                "next_link_offset": str(start + 25) if len(detail.linked_emails) > start + 25 else "", "url": f"/cases/{record_id}"}
    row = domain.activity_record(kind, record_id)
    from app.services.customer_activities import activity_metadata
    user, _ = require_actor(service, "activities", "view")
    metadata = {key: iso_berlin_time(value) if isinstance(value, datetime) else value for key, value in activity_metadata(row).items()}
    metadata.update(ActivityResponsibility(service.db, user).flags(row))
    fields = _existing_values(kind, row)
    selected = values.get("field", "")
    start = _offset(values, "text_offset") if selected else 0
    from app.services.hub_operations import HubOperationError
    if selected and selected not in fields:
        raise HubOperationError("Das Feld ist nicht verfuegbar.")
    return {identity: str(record_id), "kind": kind, "customer_id": str(row.customer_id or ""), "lead_id": str(row.lead_id or ""),
            "metadata": metadata,
            "case_id": str(getattr(row, "case_id", None) or ""), "url": f"/activities/{kind}/{record_id}",
            "fields": [{"key": key, "value": value[start:start + 6000], "next_text_offset": str(start + 6000) if len(value) > start + 6000 else ""}
                       for key, value in fields.items() if not selected or selected == key],
            "edit_fields": {f.name: {"label": f.label, "required": f.required, "options": f.options, "multiple": f.multiple} for f in activity_fields(kind)}}


for _kind, _key, _identity in (("contacts", "contacts", "contact_id"), ("cases", "cases", "case_id"),
    ("call", "activities.calls", "activity_id"), ("task", "activities.tasks", "activity_id"), ("meeting", "activities.meetings", "activity_id")):
    register_query(HubQuery(f"{_key}.list", "Autorisierte Liste wie in der Hub-Maske, 25 Treffer pro Seite, Filter vor Seitenbildung. Keine Aenderung.",
        (Field("query", "Suchtext"), Field("customer_id", "Kunde"), Field("offset", "Seitenbeginn"))
        + ((Field("status", "Status"),) if _kind != "contacts" else ())
        + ((Field("lead_id", "Lead"), Field("view", "Benutzeransicht", options=ACTIVITY_VIEWS)) if _kind in {"call", "task", "meeting"} else ()), partial(list_records, kind=_kind)))
    register_query(HubQuery(f"{_key}.read", "Details und Felddefinitionen wie in der Hub-Maske. Lange Werte mit field und text_offset nachladen. Fall-E-Mails nur bei Zugriffsrecht, mit email_key ueber emails.read lesbar.",
        (Field(_identity, "Datensatz-ID", required=True), Field("field", "Einzelnes Feld"), Field("text_offset", "Textbeginn"))
        + ((Field("link_offset", "E-Mail-Verknuepfungsbeginn"), Field("customer_id", "Erwarteter verknuepfter Kunde (optional)")) if _kind == "cases" else ()), partial(read_record, kind=_kind)))


def assignees(service, values):
    from types import SimpleNamespace
    user, access = require_actor(service, "activities", "view")
    policy = ActivityResponsibility(service.db, user)
    from app.services.hub_operation_activities import _owner
    owner = _owner(service, values, user, access)
    choices = []
    for item in policy.choices():
        from app.services.hub_operations import HubOperationError
        try:
            policy.assignee(str(item["id"]), SimpleNamespace(**owner, assignee_user_id=None))
        except HubOperationError:
            continue
        choices.append({"user_id": str(item["id"]), "name": item["name"]})
    return page(choices, values)


register_query(HubQuery("activities.assignees", "Zulaessige aktive Verantwortliche. Kunden-/Leadrechte werden nicht durch Zuweisung erweitert.",
    (Field("customer_id", "Kunde (optional)"), Field("lead_id", "Lead (optional)"), Field("offset", "Seitenbeginn")), assignees))
