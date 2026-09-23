"""Shared deletion safeguards; kept below both HTTP and agent operations."""
import json
from email.utils import parseaddr

from sqlalchemy import select

from app.models.customer import Customer
from app.models.hub_lead import HubLead
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_scheduled_email import HubScheduledEmail


BLOCKING_MAIL_STATUSES = ("scheduled", "retrying", "sending", "failed")


def payload(cipher, encrypted):
    value = json.loads(cipher.decrypt(encrypted))
    if not isinstance(value, dict):
        raise ValueError("Die verknuepften E-Mail-Daten konnten nicht sicher geprueft werden.")
    return value


def lock_parent(db, *, kind, record_id):
    if kind not in {"leads", "customers"}:
        raise ValueError("Unbekanntes Datensatzmodul.")
    model = HubLead if kind == "leads" else Customer
    record = db.scalar(select(model).where(model.id == record_id).with_for_update().execution_options(populate_existing=True))
    if record is None:
        raise ValueError("Der verknuepfte Lead oder Kunde existiert nicht mehr.")
    return record


def mail_references(data, *, kind, record_id):
    prefix = "lead" if kind == "leads" else "customer"
    return (any(str(data.get(key) or "") == str(record_id) for key in (f"{prefix}_id", f"recipient_{prefix}_id"))
            or data.get("recipient_key") in (f"{prefix}:{record_id}", f"{kind}:{record_id}")
            or (data.get("context_module") in (kind, prefix) and str(data.get("context_record_id") or "") == str(record_id)))


def blocking_mail(db, cipher, *, kind, record, lock=False):
    addresses = set()
    if record.encrypted_profile_json:
        fields = payload(cipher, record.encrypted_profile_json).get("fields", {})
        keys = ("email", "secondary_email") if kind == "leads" else ("Email", "E_Mail", "email", "Kontakt-E-Mail", "Zweite E-Mail-Adresse")
        addresses = {parseaddr(str(fields.get(key) or ""))[1].casefold() for key in keys} - {""}
    matches = []
    query = select(HubScheduledEmail).where(HubScheduledEmail.status.in_(BLOCKING_MAIL_STATUSES)).order_by(HubScheduledEmail.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    for scheduled in db.scalars(query):
        own_id = scheduled.lead_id if kind == "leads" else scheduled.customer_id
        if own_id == record.id:
            matches.append(scheduled.id)
            continue
        # Old direct schedules have no parent ID. Do not silently ignore them;
        # an address match must be resolved by the user before deleting the parent.
        if scheduled.customer_id is None and scheduled.lead_id is None:
            data = payload(cipher, scheduled.encrypted_payload_json)
            if mail_references(data, kind=kind, record_id=record.id) or parseaddr(str(data.get("recipient_email") or ""))[1].casefold() in addresses:
                matches.append(scheduled.id)
    return matches


def require_no_scheduled_mail(db, cipher, *, kind, record):
    if blocking_mail(db, cipher, kind=kind, record=record, lock=True):
        raise ValueError("Loeschen nicht moeglich: Es sind noch geplante E-Mails vorhanden. Bitte diese zuerst in E-Mails > Geplant loeschen und danach erneut versuchen. Laufender Versand muss zuerst abgeschlossen sein.")


def detach_mail_links(db, cipher, *, kind, record_id):
    prefix = "lead" if kind == "leads" else "customer"
    # Messages, content, recipients and attachment records remain untouched.
    for model in (HubMailboxEmail, CustomerZohoEmail, HubScheduledEmail):
        candidates = []
        # These legacy references live inside encrypted JSON. Use a current,
        # locking read: a prior MySQL snapshot can predate a concurrent draft save.
        for email_id, encrypted in db.execute(select(model.id, model.encrypted_payload_json).order_by(model.id).with_for_update()):
            if mail_references(payload(cipher, encrypted), kind=kind, record_id=record_id):
                candidates.append(email_id)
        # Refresh matching ORM instances so stale identity-map data cannot undo
        # a concurrently saved draft. Unrelated encrypted payloads remain intact.
        for email_id in candidates:
            email = db.scalar(select(model).where(model.id == email_id).with_for_update().execution_options(populate_existing=True))
            if email is None:
                continue
            data = payload(cipher, email.encrypted_payload_json)
            if not mail_references(data, kind=kind, record_id=record_id):
                continue
            for key in (f"{prefix}_id", f"recipient_{prefix}_id"):
                if str(data.get(key) or "") == str(record_id):
                    data[key] = None
            if data.get("context_module") in (kind, prefix) and str(data.get("context_record_id") or "") == str(record_id):
                data["context_module"] = ""
                data["context_record_id"] = ""
            if data.get("recipient_key") in (f"{prefix}:{record_id}", f"{kind}:{record_id}"):
                data["recipient_key"] = ""
            email.encrypted_payload_json = cipher.encrypt(json.dumps(data, ensure_ascii=False))


def prepare_record_deletion(db, cipher, *, kind, record_id):
    record = lock_parent(db, kind=kind, record_id=record_id)
    require_no_scheduled_mail(db, cipher, kind=kind, record=record)
    detach_mail_links(db, cipher, kind=kind, record_id=record_id)
    return record


LEAD_DELETE_NOTICE = (
    "Endgueltig geloescht: Lead-Felder, Unterformulare, Notizen, importierte Lead-E-Mails und Aktivitaeten samt Erinnerungen.",
    "Erhalten: Angebote und separate Postfach-E-Mails/Entwuerfe samt Anhaengen; nur die Lead-Zuordnung wird entfernt.",
    "Geplante E-Mails muessen vorher separat entfernt werden. Protokolle und Agent-Kontexte bleiben bestehen.",
)
