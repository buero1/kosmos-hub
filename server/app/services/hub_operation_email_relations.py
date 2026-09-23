"""Record email history uses the same matching and visibility as CRM panels."""
from app.core.timezones import iso_berlin_time
from app.services.hub_operation_mailbox import mailbox_for, _offset
from app.services.hub_operations import HubQuery, HubOperationError, HubOperationInputField as Field, register_query
from app.services.hub_record_access import identifier


def record_emails(service, values):
    mailbox = mailbox_for(service)
    module, record_id = values["module"], identifier(values["record_id"])
    if not mailbox.scope.record_visible(module, record_id):
        raise HubOperationError("Der Datensatz ist nicht verfuegbar.")
    messages = mailbox.related_messages(module=module, record_id=record_id)
    start = _offset(values)
    return {"total": len(messages), "next_offset": str(start + 25) if len(messages) > start + 25 else "",
        "items": [{"email_key": row.key, "subject": row.subject, "direction": row.direction,
                   "date": iso_berlin_time(row.occurred_at) if row.occurred_at else ""} for row in messages[start:start + 25]]}


register_query(HubQuery("emails.for_record", "Eingegangene/gesendete E-Mails anhand aller Kontaktadressen eines Kunden, Leads oder Kontakts finden. Keine Lesemarkierung; Inhalte ueber emails.read.",
    (Field("module", "Modul", required=True, options=(("customers", "Kunden"), ("leads", "Leads"), ("contacts", "Kontakte"))),
     Field("record_id", "Datensatz-ID", required=True), Field("offset", "Seitenbeginn")), record_emails))
