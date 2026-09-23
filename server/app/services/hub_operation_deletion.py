"""Read-only consequences shared by Hub dialogs and the agent catalogue."""
import re
from sqlalchemy import func, select
from app.models.customer_activity import CustomerCallActivity, CustomerTaskActivity, CustomerMeetingActivity
from app.models.hub_lead_note import HubLeadNote
from app.models.hub_lead_email import HubLeadEmail
from app.models.hub_finance_offer import HubFinanceOffer
from app.services.hub_deletion import blocking_mail
from app.services.hub_operations import HubOperationError, HubOperationInputField, HubQuery, register_query
from app.services.hub_record_access import require_actor


def deletion_preview(service, values):
    path = values["target_path"]
    deleted, retained, blockers = [], [], []
    match = re.fullmatch(r"/(leads|customers)/([1-9]\d*)/delete", path)
    if match:
        from app.services.hub_operation_records import lead_detail, customer_detail
        kind, record_id = match[1], int(match[2])
        detail = (lead_detail if kind == "leads" else customer_detail)(service, {"lead_id" if kind == "leads" else "customer_id": str(record_id)}, action="delete")
        record = detail.lead if kind == "leads" else detail.entry.customer
        scheduled = blocking_mail(service.db, service.cipher, kind=kind, record=record)
        if scheduled:
            blockers.append(f"Noch {len(scheduled)} geplante E-Mail(s). Bitte zuerst unter E-Mails > Geplant loeschen. Laufender Versand muss zuerst abgeschlossen sein.")
        if kind == "customers":
            blockers.append("Das Loeschen ganzer Kunden ist derzeit nicht freigegeben.")
        else:
            deleted.append("Lead mit allen Feldern und Unterformularen, einschliesslich Leadergebnis-Verlauf")
            for model, label in ((HubLeadNote, "Notizen"), (HubLeadEmail, "direkt gespeicherte/importierte Lead-E-Mails"),
                                 (CustomerCallActivity, "Anrufe"), (CustomerTaskActivity, "Aufgaben"), (CustomerMeetingActivity, "Meetings")):
                count = service.db.scalar(select(func.count()).select_from(model).where(model.lead_id == record_id))
                deleted.append(f"{count} {label}")
            deleted.append("Erinnerungen zu diesen Anrufen/Meetings; Aufgaben-Erinnerungen werden nicht mehr versendet")
            offers = service.db.scalar(select(func.count()).select_from(HubFinanceOffer).where(HubFinanceOffer.lead_id == record_id))
            retained.extend((f"{offers} Angebote mit Positionen und PDFs; nur die Lead-Verknuepfung entfaellt",
                             "Separate E-Mails und Entwuerfe samt Anhaengen; nur die Lead-Zuordnung entfaellt",
                             "Protokolle und bereits gespeicherte Agent-Kontexte"))
    elif match := re.fullmatch(r"/finance/(articles|offers|orders|invoices|dunnings|recurring-invoices)/([1-9]\d*)/delete", path):
        from app.services.hub_finance_operations_shared import require_record
        kind = match[1]
        require_record(service, kind, int(match[2]), "delete")
        if kind == "articles":
            deleted.append("Artikelstammsatz")
            retained.append("Bereits gespeicherte Belegpositionen; ihre Artikelverknuepfung entfaellt")
        else:
            deleted.append("Dieser Beleg mit allen Positionen")
            if kind != "recurring-invoices":
                deleted.append("Die zu diesem Beleg gespeicherten PDF-Dateien")
            retained.extend(("Kunde, Lead und Ansprechpartner", "Andere Finanzbelege; Verknuepfungen zu diesem Beleg entfallen", "E-Mails und deren eigene Anhang-Kopien"))
            if kind == "recurring-invoices":
                retained.append("Bereits erzeugte Rechnungen bleiben erhalten; aus der geloeschten Serie entstehen keine neuen Rechnungen")
    elif match := re.fullmatch(r"/contacts/([1-9]\d*)/delete", path):
        from app.services.hub_operation_contacts import _contact
        from app.services.customer_directory import CustomerDirectoryService
        user, access = require_actor(service, "contacts", "delete")
        _contact(service, CustomerDirectoryService(db=service.db, cipher=service.cipher), access, user, {"contact_id": match[1]})
        deleted.append("Kontakt mit seinen Feldern und Unterformularen")
        retained.append("Kunde, E-Mails und Finanzbelege; Ansprechpartner-Verknuepfungen entfallen")
    elif match := re.fullmatch(r"/cases/([1-9]\d*)/delete", path):
        from app.services.hub_operation_cases import _case
        user, access = require_actor(service, "cases", "delete")
        _case(service, user, access, {"case_id": match[1]})
        deleted.extend(("Fall mit seinen Feldern", "Zugehoerige Aufgaben und Fall-Erinnerungen", "Verknuepfungen zu E-Mails"))
        retained.append("Die E-Mails selbst samt Anhaengen sowie der Kunde")
    elif match := re.fullmatch(r"/(leads|customers)/([1-9]\d*)/activities/(calls|tasks|meetings)/([1-9]\d*)/delete", path):
        from app.services.hub_operation_activities import _activity
        user, access = require_actor(service, "activities", "delete")
        owner_key = "lead_id" if match[1] == "leads" else "customer_id"
        _activity(service, {"calls": "call", "tasks": "task", "meetings": "meeting"}[match[3]], {"activity_id": match[4]}, {owner_key: int(match[2])}, user, access)
        deleted.append("Diese Aktivitaet mit ihren Angaben und Erinnerungen; Aufgaben-Erinnerungen werden gestoppt")
        retained.append("Zugehoeriger Kunde oder Lead sowie andere Aktivitaeten")
    elif match := re.fullmatch(r"/(leads|customers)/([1-9]\d*)/(?:communications/)?notes/([1-9]\d*)/delete", path):
        from app.models.customer_communication import CustomerZohoNote
        from app.services.hub_operation_records import lead_detail, customer_detail
        kind, record_id, note_id = match[1], int(match[2]), int(match[3])
        parent_key = "lead_id" if kind == "leads" else "customer_id"
        (lead_detail if kind == "leads" else customer_detail)(service, {parent_key: str(record_id)}, action="delete")
        note = service.db.get(HubLeadNote if kind == "leads" else CustomerZohoNote, note_id)
        if note is None or getattr(note, parent_key) != record_id:
            raise HubOperationError("Die Notiz wurde bei diesem Datensatz nicht gefunden.")
        deleted.append("Diese Notiz mit Titel und Inhalt")
        retained.append("Kunde bzw. Lead, andere Notizen, E-Mails und Aktivitaeten")
    else:
        raise HubOperationError("Fuer diese Aktion ist keine Loeschvorschau verfuegbar.")
    return {"deleted": deleted, "retained": retained, "blockers": blockers,
            "planned_url": "/emails?folder=planned", "permanent": True}


register_query(HubQuery(
    key="records.deletion_preview",
    description="Zeigt mitgeloeschte und erhaltene Bausteine sowie blockierenden geplanten Versand vor einer Loeschung. Veraendert keine Daten.",
    input_fields=(HubOperationInputField("target_path", "Loeschpfad des Datensatzes", required=True, max_length=255),),
    execute=deletion_preview,
))
