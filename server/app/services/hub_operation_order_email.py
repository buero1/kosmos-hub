"""Manual order mail: editable customer-linked draft with a verified PDF."""

import json
import re
from hashlib import sha256
from urllib.parse import urlencode

from sqlalchemy import select

from app.models.hub_finance_documents import HubFinanceOrder, HubFinanceOrderLine
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.audit import write_audit_log
from app.services.customer_communications import CustomerCommunicationRecipient
from app.services.hub_email_composition import mailbox_for, render_template
from app.services.hub_finance_operations_shared import require_record
from app.services.hub_finance_pdf_readers import load_pdf
from app.services.hub_operation_email_drafts import _save_draft
from app.services.hub_operation_invoice_email import SEND_FIELDS
from app.services.hub_operations import (
    HubArtifact, HubOperation, HubOperationError, HubOperationInputField as Field,
    HubOperationResult, HubOperationService, register_operation,
)
from app.services.hub_record_access import identifier, require_actor


def require_editable_order_draft(metadata):
    if metadata.get("state") not in (None, "ready", "failed"):
        raise HubOperationError("Dieser Auftragsversand läuft oder hat einen unklaren Status. Bitte zuerst den Versand prüfen; nicht erneut senden.")


def order_draft_metadata(service, draft_id):
    if not draft_id:
        return None
    mailbox = mailbox_for(service)
    draft = mailbox.scope.require(f"unassigned-{identifier(str(draft_id))}")
    if draft.source != "hub-draft" or draft.mailbox_state != "draft":
        raise HubOperationError("Der Entwurf ist nicht mehr verfügbar.")
    return mailbox._payload(draft.encrypted_payload_json).get("order_dispatch")


def _fingerprint(service, order):
    lines = service.db.scalars(select(HubFinanceOrderLine).where(
        HubFinanceOrderLine.order_id == order.id,
    ).order_by(HubFinanceOrderLine.position_index).with_for_update().execution_options(populate_existing=True))
    return sha256(json.dumps([order.customer_id, order.contact_id, order.encrypted_fields_json,
        [(line.position_index, line.article_id, line.encrypted_fields_json) for line in lines]]).encode()).hexdigest()


def prepare_order_email(service, values):
    if set(values) != {"record_id"}:
        raise HubOperationError("Ungültige Auftragsauswahl.")
    require_actor(service, "emails", "create")
    require_actor(service, "emails", "edit")
    order = require_record(service, "orders", identifier(values["record_id"]))
    if not order.customer_id:
        raise HubOperationError("Bitte zuerst einen Kunden mit dem Auftrag verknüpfen und speichern.")
    mailbox = mailbox_for(service)
    sender = mailbox.scope.mailboxes.default_sender(for_send=True)
    if not sender:
        raise HubOperationError("Es ist kein freigegebenes Absenderpostfach verfügbar.")
    recipients = mailbox.communications.list_recipients(customer_id=order.customer_id)
    recipient = next((item for item in recipients if item.key.startswith(f"contact:{order.contact_id}:")), None)
    if order.contact_id and recipient is None:
        raise HubOperationError("Der Ansprechpartner hat keine E-Mail-Adresse oder gehört nicht zum Kunden.")
    recipient = recipient or next(iter(recipients), None)
    if recipient is None:
        raise HubOperationError("Bitte zuerst eine E-Mail-Adresse beim Kunden oder Ansprechpartner hinterlegen.")
    templates = [item for item in mailbox.communications.list_email_templates()
                 if item.context_module == "orders" and item.name.casefold() == "auftragsbestätigung"]
    if len(templates) != 1:
        raise HubOperationError('Die aktive Auftragsvorlage „Auftragsbestätigung“ muss genau einmal vorhanden sein.')
    values = {"customer_id": str(order.customer_id), "recipient_key": recipient.key,
              "recipient_email": recipient.email, "recipient_name": recipient.name,
              "context_module": "orders", "context_record_id": str(order.id), "template_id": templates[0].id}
    rendered = render_template(service, values)
    pdf, content = load_pdf(service, "orders", order.id, source="available")
    attachment = HubArtifact(filename=pdf.filename or f"{order.order_number}.pdf", content=content, content_type="application/pdf")
    drafts = HubOperationService(db=service.db, cipher=service.cipher, actor=service.actor, input_files=(attachment,))
    result = drafts.execute("emails.drafts.create", {**values, "sender_email": sender,
        "subject": rendered.subject, "content": rendered.content})
    draft = service.db.get(HubMailboxEmail, result.record_id)
    payload = mailbox._payload(draft.encrypted_payload_json)
    payload["order_dispatch"] = {"order_id": order.id, "customer_id": order.customer_id,
        "fingerprint": _fingerprint(service, order), "pdf_sha256": sha256(content).hexdigest(), "state": "ready"}
    draft.encrypted_payload_json = service.cipher.encrypt(json.dumps(payload, ensure_ascii=False))
    service.db.flush()
    return result


def send_order_email(service, values):
    if set(values) - set(SEND_FIELDS):
        raise HubOperationError("Unbekannte Versandeingabe.")
    require_actor(service, "emails", "create")
    require_actor(service, "emails", "edit")
    mailbox = mailbox_for(service)
    draft_id = identifier(values.get("draft_id", ""))
    mailbox.scope.require(f"unassigned-{draft_id}")
    draft = service.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.id == draft_id)
        .with_for_update().execution_options(populate_existing=True))
    metadata = order_draft_metadata(service, draft_id)
    if not metadata:
        raise HubOperationError("Kein vorbereiteter Auftragsentwurf vorhanden.")
    require_editable_order_draft(metadata)
    order_id = metadata["order_id"]
    service.db.scalar(select(HubFinanceOrder).where(HubFinanceOrder.id == order_id)
        .with_for_update().execution_options(populate_existing=True))
    order = require_record(service, "orders", order_id)
    if order.customer_id != metadata["customer_id"] or _fingerprint(service, order) != metadata["fingerprint"]:
        raise HubOperationError("Der Auftrag hat sich geändert. Bitte den Versand aus dem Auftrag neu vorbereiten.")
    if any(values.get(key) for key in ("scheduled_at", "lead_id", "dunning_id", "reply_to_email_id", "forward_from_email_id")):
        raise HubOperationError("Dieser Auftragsentwurf kann nur direkt und manuell gesendet werden.")
    if values.get("recipient_customer_id") not in (None, "", str(order.customer_id)):
        raise HubOperationError("Der Entwurf gehört zu einem anderen Kunden.")
    _pdf, content = load_pdf(service, "orders", order.id, source="available")
    if sha256(content).hexdigest() != metadata["pdf_sha256"]:
        raise HubOperationError("Die Auftrags-PDF hat sich geändert. Bitte den Versand neu vorbereiten.")
    _save_draft(service, {**values, "customer_id": str(order.customer_id),
        "context_module": "orders", "context_record_id": str(order.id)}, complete=True)
    context = mailbox.get_draft_compose_context(draft_id=draft_id)
    unresolved = sorted(set(re.findall(r"\$\{[^{}]+\}", context["subject"] + " " + context["content"])))
    if unresolved:
        raise HubOperationError("Bitte die offenen Platzhalter vor dem Senden ergänzen oder entfernen: " + ", ".join(unresolved[:4]))
    mailbox.scope.mailboxes.require_sender(context["sender_email"], "send")
    attachments = mailbox.prepare_draft_delivery_attachments(draft_id=draft_id, retained_attachment_ids=None)
    if not any(sha256(item.content).hexdigest() == metadata["pdf_sha256"] for item in attachments):
        raise HubOperationError("Die Auftrags-PDF fehlt im Anhang. Bitte den Versand neu vorbereiten.")
    recipient = next((item for item in mailbox.communications.list_recipients(customer_id=order.customer_id)
                      if item.email.casefold() == context["recipient_email"].casefold()), None)
    recipient = recipient or CustomerCommunicationRecipient(key="", name=values.get("recipient_name") or context["recipient_email"],
                                                            email=context["recipient_email"])
    payload = mailbox._payload(draft.encrypted_payload_json)

    def claim(state):
        payload["order_dispatch"]["state"] = state
        draft.encrypted_payload_json = service.cipher.encrypt(json.dumps(payload, ensure_ascii=False))
        service.db.commit()

    # Persist the claim before SMTP; an unknown outcome must not trigger a second send.
    claim("sending")
    try:
        sent = mailbox.communications.send_email(customer_id=order.customer_id, actor=service.actor,
            sender_email=context["sender_email"], recipient_key=recipient.key, recipient_override=recipient,
            subject=context["subject"], content=context["content"], template_id=context["template_id"],
            cc_emails=context["cc_emails"], attachments=attachments)
    except Exception as exc:
        service.db.rollback()
        claim("uncertain")
        raise HubOperationError("Der Versandstatus ist unklar. Bitte das Postfach prüfen; nicht erneut senden.") from exc
    if not sent.success:
        claim("failed")
        raise HubOperationError("Der Versand ist fehlgeschlagen. Der Entwurf bleibt erhalten.")
    mailbox.discard_draft(draft_id=draft_id)
    write_audit_log(service.db, site=None, actor=service.actor, source="hub-web",
        action="send-order-email", result="ok", detail=f"Order {order.id}; customer email {sent.email_id}; no email content logged.")
    service.db.commit()
    query = urlencode({"email": "success", "email_message": "Auftrag wurde per E-Mail versendet und beim Kunden gespeichert."})
    return HubOperationResult("Auftrag öffnen", f"/finance/orders/{order.id}?{query}", order.id)


register_operation(HubOperation(key="finance.orders.email.prepare", module="finance", label="Auftrags-E-Mail vorbereiten",
    description="Erstellt einen Kunden-E-Mail-Entwurf mit Auftragsvorlage und fertiger PDF. Kein Versand.",
    input_guide="record_id des Auftrags; Kunde, E-Mail und fertige PDF erforderlich.",
    input_fields=lambda: (Field("record_id", "Auftrags-ID", required=True),), preview_fields=(("record_id", "Auftrag"),),
    execute=prepare_order_email))
register_operation(HubOperation(key="finance.orders.email.send", module="finance", label="Auftrags-E-Mail senden",
    description="Versendet einen manuell bestätigten Auftragsentwurf und verknüpft ihn mit dem Kunden.",
    input_guide="Vorbereiteter Entwurf und manuell geprüfte E-Mail-Felder.", preview_fields=(),
    execute=send_order_email, agent_enabled=False))
