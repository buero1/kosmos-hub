"""Shared, unsent email draft operation with optional Hub artifact attachments."""

from __future__ import annotations

from email.utils import parseaddr
from typing import Mapping

from sqlalchemy import select

from app.core.config import get_settings
from app.models.hub_user import HubUser
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_finance_documents import HubFinanceDunning
from app.models.customer import Customer
from app.models.hub_lead import HubLead
from app.services.customer_communications import CustomerCommunicationAttachmentUpload
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_transport import DEFAULT_HUB_MAILBOX_SENDER_EMAIL
from app.services.hub_operations import (
    HubOperation, HubOperationError, HubOperationInputField, HubOperationResult, HubOperationService, register_operation,
)


def _optional_id(value: str) -> int | None:
    if not value:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise HubOperationError("Die Datensatz-Verknüpfung ist ungültig.")
    return int(value)


def _save_draft(service: HubOperationService, values: Mapping[str, str], *, complete: bool, preserve_existing_links: bool = True) -> HubOperationResult:
    if values.get("context_module") or values.get("context_record_id"):
        from app.services.hub_template_contexts import resolve_context
        values, _ = resolve_context(service, {**values, "customer_id": values.get("customer_id") or values.get("recipient_customer_id") or ""})
    user = service.db.scalar(select(HubUser).where(HubUser.username == service.actor, HubUser.is_active.is_(True)))
    required_action = "edit" if values.get("draft_id") else "create"
    if user is None or not HubAccessControlService(db=service.db).can(user, "emails", "view") or not HubAccessControlService(db=service.db).can(user, "emails", required_action):
        raise HubOperationError("E-Mail-Entwürfe dürfen mit diesem Benutzer nicht angelegt werden.")
    recipient = values.get("recipient_email", "").strip()
    address = parseaddr(recipient)[1]
    if complete and recipient and (not address or address != recipient or "@" not in address):
        raise HubOperationError("Eine gültige Empfängeradresse ist erforderlich.")
    if complete and not recipient:
        raise HubOperationError("Eine Empfängeradresse ist erforderlich.")
    subject = values.get("subject", "").strip()
    content = values.get("content", "").strip()
    if complete and (not subject or not content):
        raise HubOperationError("Betreff und E-Mail-Inhalt sind erforderlich.")
    access = HubAccessControlService(db=service.db)
    customer_id = _optional_id((values.get("customer_id") or values.get("recipient_customer_id") or "").strip())
    lead_id = _optional_id(values.get("lead_id", "").strip())
    draft_id = _optional_id(values.get("draft_id", "").strip())
    dunning_id = _optional_id(values.get("dunning_id", "").strip())
    if customer_id is not None and (not access.can_access_record(user=user, module_key="customers", record_id=customer_id) or service.db.get(Customer, customer_id) is None):
        raise HubOperationError("Der Kunde ist nicht verfügbar.")
    if lead_id is not None and (not access.can_access_record(user=user, module_key="leads", record_id=lead_id) or service.db.get(HubLead, lead_id) is None):
        raise HubOperationError("Der Lead ist nicht verfügbar.")
    if dunning_id is not None:
        dunning = service.db.get(HubFinanceDunning, dunning_id)
        if dunning is None or not access.can_access_record(user=user, module_key="customers", record_id=dunning.customer_id) or (customer_id is not None and customer_id != dunning.customer_id):
            raise HubOperationError("Die verknuepfte Mahnung ist nicht verfuegbar.")
    artifact_ref = values.get("attachment_ref", "").strip()
    attachment = service.load_artifact(artifact_ref) if artifact_ref else None
    if attachment is not None and attachment.recipient_email and attachment.recipient_email.casefold() != recipient.casefold():
        raise HubOperationError("Die Empfängeradresse stimmt nicht mit dem Angebotskontakt überein.")
    input_files = service.input_files + ((attachment,) if attachment is not None else ())
    mailbox = HubMailboxService(db=service.db, cipher=service.cipher, public_base_url=get_settings().public_base_url, actor=service.actor)
    draft = mailbox.save_draft(
        draft_id=draft_id,
        sender_email=values.get("sender_email") or mailbox.scope.mailboxes.default_sender(),
        recipient_email=recipient,
        recipient_key=values.get("recipient_key", ""),
        recipient_customer_id=customer_id,
        recipient_lead_id=lead_id,
        recipient_name=values.get("recipient_name", ""),
        subject=subject,
        content=mailbox.communications._sanitized_email_content(content) if complete else content,
        cc_emails=values.get("cc_emails", ""), template_id=values.get("template_id", ""),
        reply_to_email_id=values.get("reply_to_email_id", ""),
        forward_from_email_id=values.get("forward_from_email_id", ""),
        dunning_id=dunning_id, scheduled_at=values.get("scheduled_at", ""),
        context_module=values.get("context_module", ""), context_record_id=values.get("context_record_id", ""),
        attachments=tuple(CustomerCommunicationAttachmentUpload(
            filename=item.filename, content=item.content, content_type=item.content_type,
        ) for item in input_files),
        retained_attachment_ids=mailbox.parse_retained_attachment_ids(values.get("retained_attachment_ids", "")),
        preserve_existing_links=preserve_existing_links,
    )
    return HubOperationResult(
        label="E-Mail-Entwurf öffnen", href=f"/emails?folder=drafts&selected=unassigned-{draft.id}",
        record_id=draft.id,
    )


def _create_draft(service: HubOperationService, values: Mapping[str, str]) -> HubOperationResult:
    return _save_draft(service, values, complete=True)


def _save_human_draft(service: HubOperationService, values: Mapping[str, str]) -> HubOperationResult:
    return _save_draft(service, values, complete=False)


_UPDATE_FIELDS = (
    HubOperationInputField("draft_id", "Entwurfs-ID", required=True),
    HubOperationInputField("sender_email", "Absender", max_length=320),
    HubOperationInputField("recipient_email", "Empfaenger", max_length=320),
    HubOperationInputField("recipient_name", "Empfaengername", max_length=255),
    HubOperationInputField("subject", "Betreff", max_length=500),
    HubOperationInputField("content", "Nachricht (HTML)", max_length=500_000),
    HubOperationInputField("cc_emails", "CC", max_length=2_000),
    HubOperationInputField("customer_id", "Kunden-ID", context_type="customer"),
    HubOperationInputField("lead_id", "Lead-ID", context_type="lead"),
    HubOperationInputField("attachment_ref", "Zusaetzlicher freigegebener Hub-Anhang"),
    HubOperationInputField("retained_attachment_ids", "Zu behaltende Anhang-IDs; nur zum Entfernen angeben", encoding="JSON array of strings"),
)


def _update_draft(service, values):
    if set(values) - {field.name for field in _UPDATE_FIELDS}:
        raise HubOperationError("Unbekannte Entwurfseingabe. Versandplanung ist hier nicht erlaubt.")
    for field in _UPDATE_FIELDS:
        if field.max_length and len(values.get(field.name, "")) > field.max_length:
            raise HubOperationError(f"{field.label} ist zu lang.")
    draft_id = _optional_id(values.get("draft_id", ""))
    if draft_id is None:
        raise HubOperationError("Entwurfs-ID fehlt.")
    mailbox = HubMailboxService(db=service.db, cipher=service.cipher, public_base_url=get_settings().public_base_url, actor=service.actor)
    mailbox.scope.require(f"unassigned-{draft_id}")
    service.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.id == draft_id).with_for_update())
    context = mailbox.get_draft_compose_context(draft_id=draft_id)
    record = mailbox.scope.require(f"unassigned-{draft_id}")
    payload = mailbox._payload(record.encrypted_payload_json)
    merged = {key: str(payload.get(key) or "") for key in (
        "sender_email", "recipient_email", "recipient_name", "recipient_key", "subject", "content", "cc_emails", "template_id", "reply_to_email_id", "forward_from_email_id", "dunning_id", "scheduled_at", "context_module", "context_record_id",
    )}
    merged.update(sender_email=context["sender_email"], customer_id=str(context["customer_id"] or ""), lead_id=str(context["lead_id"] or ""))
    merged.update(values)
    if any(key in values and values[key] != str(context.get(key) or "") for key in ("customer_id", "recipient_email")):
        merged["recipient_key"] = ""
    return _save_draft(service, merged, complete=False, preserve_existing_links=False)


register_operation(HubOperation(
    key="emails.drafts.update", module="emails", label="E-Mail-Entwurf bearbeiten",
    description="Bearbeitet einen gespeicherten Entwurf wie die E-Mail-Maske. Kein Versand.",
    input_guide="draft_id erforderlich. Ausgelassene Felder, Verknuepfungen und Anhaenge bleiben erhalten. Explizit leerer Text leert ein Feld. Zum Entfernen von Anhaengen retained_attachment_ids als JSON-Liste der verbleibenden IDs angeben. Vorher emails.drafts.read nutzen; keine Versandplanung.",
    preview_fields=tuple((field.name, field.label) for field in _UPDATE_FIELDS),
    input_fields=lambda: _UPDATE_FIELDS, execute=_update_draft,
))


register_operation(HubOperation(
    key="emails.drafts.create",
    module="emails",
    label="E-Mail-Entwurf anlegen",
    description="Speichert einen unversendeten E-Mail-Entwurf, optional mit einem freigegebenen Hub-Artefakt als Anhang.",
    input_guide=(
        "recipient_email, subject und content (HTML) sind erforderlich; optional recipient_name, customer_id, "
        "lead_id und attachment_ref. Für Ergebnisse voriger Aktionen nutze exakt "
        "{{action.1.recipient_email}}, {{action.1.artifact_ref}} und {{action.1.lead_id}} "
        "(1 ist die Nummer der vorigen Aktion im selben Plan). Alternativ artifact_ref aus emails.attachments.list verwenden. Eine neu erzeugte PDF muss fertig sein. "
        "Kein Versand und keine automatische Versandplanung."
    ),
    preview_fields=(("recipient_email", "An"), ("subject", "Betreff"), ("attachment_ref", "Anhang")),
    execute=_create_draft,
))

register_operation(HubOperation(
    key="emails.drafts.save",
    module="emails",
    label="E-Mail-Entwurf speichern",
    description="Speichert einen manuell bearbeiteten E-Mail-Entwurf.",
    input_guide="Entwurfsfelder und optional draft_id.",
    preview_fields=(),
    execute=_save_human_draft,
    agent_enabled=False,
))
