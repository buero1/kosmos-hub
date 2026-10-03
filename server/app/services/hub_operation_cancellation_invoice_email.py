"""Manual cancellation-invoice mail with a verified immutable ZUGFeRD PDF."""

import json
import re
from hashlib import sha256
from urllib.parse import urlencode

from sqlalchemy import select

from app.models.hub_finance_documents import (
    HubFinanceCancellationInvoice,
    HubFinanceCancellationInvoiceLine,
)
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.audit import write_audit_log
from app.services.customer_communications import CustomerCommunicationRecipient
from app.services.hub_email_composition import mailbox_for, render_template
from app.services.hub_finance_documents import CANCELLATION_INVOICE_MODULE, HubFinanceDocumentService
from app.services.hub_finance_operations_shared import require_record
from app.services.hub_finance_pdf_readers import load_pdf
from app.services.hub_operation_email_drafts import _save_draft
from app.services.hub_operation_invoice_email import SEND_FIELDS
from app.services.hub_operations import (
    HubArtifact,
    HubOperation,
    HubOperationError,
    HubOperationInputField as Field,
    HubOperationResult,
    HubOperationService,
    register_operation,
)
from app.services.hub_record_access import identifier, require_actor


def require_editable_cancellation_draft(metadata):
    if metadata.get("state") not in (None, "ready", "failed"):
        raise HubOperationError(
            "Dieser Stornorechnungsversand läuft oder hat einen unklaren Status. Bitte zuerst das Postfach prüfen; nicht erneut senden."
        )


def cancellation_draft_metadata(service, draft_id):
    if not draft_id:
        return None
    mailbox = mailbox_for(service)
    draft = mailbox.scope.require(f"unassigned-{identifier(str(draft_id))}")
    if draft.source != "hub-draft" or draft.mailbox_state != "draft":
        raise HubOperationError("Der Entwurf ist nicht mehr verfügbar.")
    return mailbox._payload(draft.encrypted_payload_json).get("cancellation_dispatch")


def _fingerprint(service, cancellation):
    lines = service.db.scalars(
        select(HubFinanceCancellationInvoiceLine)
        .where(HubFinanceCancellationInvoiceLine.cancellation_invoice_id == cancellation.id)
        .order_by(HubFinanceCancellationInvoiceLine.position_index)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return sha256(json.dumps([
        cancellation.customer_id,
        cancellation.contact_id,
        cancellation.invoice_id,
        cancellation.encrypted_fields_json,
        [(line.position_index, line.article_id, line.encrypted_fields_json) for line in lines],
    ]).encode()).hexdigest()


def _template(templates):
    matches = [
        item for item in templates
        if item.context_module in {"cancellation-invoices", "customers", "general"}
        and item.name.strip().casefold() == "stornorechnung"
    ]
    dedicated = [item for item in matches if item.context_module == "cancellation-invoices"]
    candidates = dedicated or matches
    return candidates[0] if len(candidates) == 1 else None


def prepare_cancellation_email(service, values):
    if set(values) != {"record_id"}:
        raise HubOperationError("Ungültige Stornorechnungsauswahl.")
    require_actor(service, "emails", "create")
    require_actor(service, "emails", "edit")
    cancellation = require_record(service, "cancellation-invoices", identifier(values["record_id"]))
    document_service = HubFinanceDocumentService(db=service.db, cipher=service.cipher)
    stored = document_service._document_values(module=CANCELLATION_INVOICE_MODULE, document=cancellation)
    if stored.get("status") not in {"created", "sent"}:
        raise HubOperationError("Die Stornorechnung ist noch nicht fertiggestellt.")
    if not cancellation.customer_id:
        raise HubOperationError("Bitte zuerst einen Kunden mit der Stornorechnung verknüpfen.")
    mailbox = mailbox_for(service)
    sender = mailbox.scope.mailboxes.default_sender(for_send=True)
    if not sender:
        raise HubOperationError("Es ist kein freigegebenes Absenderpostfach verfügbar.")
    recipients = mailbox.communications.list_recipients(customer_id=cancellation.customer_id)
    recipient = next((item for item in recipients if item.key.startswith(f"contact:{cancellation.contact_id}:")), None)
    if cancellation.contact_id and recipient is None:
        raise HubOperationError("Der Ansprechpartner hat keine E-Mail-Adresse oder gehört nicht zum Kunden.")
    recipient = recipient or next(iter(recipients), None)
    if recipient is None:
        raise HubOperationError("Bitte zuerst eine E-Mail-Adresse beim Kunden oder Ansprechpartner hinterlegen.")
    template = _template(mailbox.communications.list_email_templates())
    context = {
        "customer_id": str(cancellation.customer_id),
        "recipient_key": recipient.key,
        "recipient_email": recipient.email,
        "recipient_name": recipient.name,
        "context_module": "cancellation-invoices",
        "context_record_id": str(cancellation.id),
        "template_id": template.id if template else "",
    }
    rendered = render_template(service, context) if template else None
    pdf, content = load_pdf(service, "cancellation-invoices", cancellation.id, source="available")
    attachment = HubArtifact(
        filename=pdf.filename or f"{cancellation.cancellation_number}.pdf",
        content=content,
        content_type="application/pdf",
    )
    drafts = HubOperationService(
        db=service.db,
        cipher=service.cipher,
        actor=service.actor,
        input_files=(attachment,),
    )
    result = drafts.execute("emails.drafts.save", {
        **context,
        "sender_email": sender,
        "subject": rendered.subject if rendered else f"Stornorechnung {cancellation.cancellation_number}",
        "content": rendered.content if rendered else "",
    })
    draft = service.db.get(HubMailboxEmail, result.record_id)
    payload = mailbox._payload(draft.encrypted_payload_json)
    payload["cancellation_dispatch"] = {
        "cancellation_id": cancellation.id,
        "customer_id": cancellation.customer_id,
        "fingerprint": _fingerprint(service, cancellation),
        "pdf_key": pdf.storage_key,
        "pdf_sha256": sha256(content).hexdigest(),
        "state": "ready",
    }
    draft.encrypted_payload_json = service.cipher.encrypt(json.dumps(payload, ensure_ascii=False))
    service.db.flush()
    return result


def send_cancellation_email(service, values):
    if set(values) - set(SEND_FIELDS):
        raise HubOperationError("Unbekannte Versandeingabe.")
    require_actor(service, "emails", "create")
    require_actor(service, "emails", "edit")
    mailbox = mailbox_for(service)
    draft_id = identifier(values.get("draft_id", ""))
    mailbox.scope.require(f"unassigned-{draft_id}")
    draft = service.db.scalar(
        select(HubMailboxEmail)
        .where(HubMailboxEmail.id == draft_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    metadata = cancellation_draft_metadata(service, draft_id)
    if not metadata:
        raise HubOperationError("Kein vorbereiteter Stornorechnungsentwurf vorhanden.")
    require_editable_cancellation_draft(metadata)
    cancellation_id = metadata["cancellation_id"]
    service.db.scalar(
        select(HubFinanceCancellationInvoice)
        .where(HubFinanceCancellationInvoice.id == cancellation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    cancellation = require_record(service, "cancellation-invoices", cancellation_id)
    if cancellation.customer_id != metadata["customer_id"] or _fingerprint(service, cancellation) != metadata["fingerprint"]:
        raise HubOperationError("Die Stornorechnung hat sich geändert. Bitte den Versand neu vorbereiten.")
    if any(values.get(key) for key in ("scheduled_at", "lead_id", "dunning_id", "reply_to_email_id", "forward_from_email_id")):
        raise HubOperationError("Dieser Stornorechnungsentwurf kann nur direkt und manuell gesendet werden.")
    if values.get("recipient_customer_id") not in (None, "", str(cancellation.customer_id)):
        raise HubOperationError("Der Entwurf gehört zu einem anderen Kunden.")
    pdf, content = load_pdf(service, "cancellation-invoices", cancellation.id, source="available")
    if pdf.storage_key != metadata["pdf_key"] or sha256(content).hexdigest() != metadata["pdf_sha256"]:
        raise HubOperationError("Die Stornorechnungs-PDF hat sich geändert. Bitte den Versand neu vorbereiten.")
    _save_draft(service, {
        **values,
        "customer_id": str(cancellation.customer_id),
        "context_module": "cancellation-invoices",
        "context_record_id": str(cancellation.id),
    }, complete=True)
    context = mailbox.get_draft_compose_context(draft_id=draft_id)
    unresolved = sorted(set(re.findall(r"\$\{[^{}]+\}", context["subject"] + " " + context["content"])))
    if unresolved:
        raise HubOperationError(
            "Bitte die offenen Platzhalter vor dem Senden ergänzen oder entfernen: " + ", ".join(unresolved[:4])
        )
    mailbox.scope.mailboxes.require_sender(context["sender_email"], "send")
    attachments = mailbox.prepare_draft_delivery_attachments(draft_id=draft_id, retained_attachment_ids=None)
    if not any(sha256(item.content).hexdigest() == metadata["pdf_sha256"] for item in attachments):
        raise HubOperationError("Die Stornorechnungs-PDF fehlt im Anhang. Bitte den Versand neu vorbereiten.")
    recipient = next((item for item in mailbox.communications.list_recipients(customer_id=cancellation.customer_id)
                      if item.email.casefold() == context["recipient_email"].casefold()), None)
    recipient = recipient or CustomerCommunicationRecipient(
        key="",
        name=values.get("recipient_name") or context["recipient_email"],
        email=context["recipient_email"],
    )
    payload = mailbox._payload(draft.encrypted_payload_json)

    def claim(state):
        payload["cancellation_dispatch"]["state"] = state
        draft.encrypted_payload_json = service.cipher.encrypt(json.dumps(payload, ensure_ascii=False))
        service.db.commit()

    claim("sending")
    try:
        sent = mailbox.communications.send_email(
            customer_id=cancellation.customer_id,
            actor=service.actor,
            sender_email=context["sender_email"],
            recipient_key=recipient.key,
            recipient_override=recipient,
            subject=context["subject"],
            content=context["content"],
            template_id=context["template_id"],
            cc_emails=context["cc_emails"],
            attachments=attachments,
        )
    except Exception as exc:
        service.db.rollback()
        claim("uncertain")
        raise HubOperationError("Der Versandstatus ist unklar. Bitte das Postfach prüfen; nicht erneut senden.") from exc
    if not sent.success:
        claim("failed")
        raise HubOperationError("Der Versand ist fehlgeschlagen. Der Entwurf bleibt erhalten.")
    HubFinanceDocumentService(db=service.db, cipher=service.cipher).mark_cancellation_sent(
        cancellation_id=cancellation.id,
    )
    mailbox.discard_draft(draft_id=draft_id)
    write_audit_log(
        service.db,
        site=None,
        actor=service.actor,
        source="hub-web",
        action="send-cancellation-invoice-email",
        result="ok",
        detail=f"Cancellation invoice {cancellation.id}; customer email {sent.email_id}; no email content logged.",
    )
    service.db.commit()
    query = urlencode({
        "email": "success",
        "email_message": "Stornorechnung wurde per E-Mail versendet und beim Kunden gespeichert.",
    })
    return HubOperationResult(
        "Stornorechnung öffnen",
        f"/finance/cancellation-invoices/{cancellation.id}?{query}",
        cancellation.id,
    )


register_operation(HubOperation(
    key="finance.cancellation-invoices.email.prepare",
    module="finance",
    label="Stornorechnungs-E-Mail vorbereiten",
    description="Erstellt einen Kunden-E-Mail-Entwurf mit Stornorechnungsvorlage und fertiger ZUGFeRD-PDF. Kein Versand.",
    input_guide="record_id der Stornorechnung; Kunde, E-Mail und fertige PDF erforderlich.",
    input_fields=lambda: (Field("record_id", "Stornorechnungs-ID", required=True),),
    preview_fields=(("record_id", "Stornorechnung"),),
    execute=prepare_cancellation_email,
))
register_operation(HubOperation(
    key="finance.cancellation-invoices.email.send",
    module="finance",
    label="Stornorechnungs-E-Mail senden",
    description="Versendet einen manuell bestätigten Stornorechnungsentwurf und verknüpft ihn mit dem Kunden.",
    input_guide="Vorbereiteter Entwurf und manuell geprüfte E-Mail-Felder.",
    preview_fields=(),
    execute=send_cancellation_email,
    agent_enabled=False,
))
