"""Editable invoice mail using normal drafts and the shared delivery journal."""

import json
import logging
from hashlib import sha256
from secrets import token_urlsafe

from sqlalchemy import select

from app.models.base import utcnow
from app.models.hub_finance_documents import HubFinanceInvoice, HubFinanceInvoiceLine
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.models.hub_invoice_email_batch import HubInvoiceEmailBatch, HubInvoiceEmailBatchItem
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.audit import write_audit_log
from app.services.finance_generated_pdf_storage import FinanceGeneratedPdfStorage, FinanceGeneratedPdfStorageError
from app.services.finance_invoice_pdf_storage import FinanceInvoicePdfStorage, FinanceInvoicePdfStorageError
from app.services.hub_email_composition import mailbox_for
from app.services.hub_finance_operations_shared import require_record
from app.services.hub_invoice_email_batches import HubInvoiceEmailBatchService
from app.services.hub_operation_email_drafts import _save_draft
from app.services.hub_operations import HubArtifact, HubOperation, HubOperationError, HubOperationInputField as Field, HubOperationResult, HubOperationService, register_operation
from app.services.hub_record_access import identifier, require_actor


def require_editable_invoice_draft(db, metadata):
    if metadata.get("batch_id"):
        item = db.scalar(select(HubInvoiceEmailBatchItem).where(
            HubInvoiceEmailBatchItem.batch_id == metadata["batch_id"],
        ).with_for_update().execution_options(populate_existing=True))
        if item and item.status != "failed":
            raise HubOperationError("Dieser Rechnungsversand läuft, ist bereits erfolgt oder hat einen unklaren Status. Bitte zuerst den Versandstatus prüfen.")


def invoice_draft_metadata(service, draft_id):
    if not draft_id:
        return None
    mailbox = mailbox_for(service)
    draft = mailbox.scope.require(f"unassigned-{identifier(str(draft_id))}")
    if draft.source != "hub-draft" or draft.mailbox_state != "draft":
        raise HubOperationError("Der Entwurf ist nicht mehr verfügbar.")
    return mailbox._payload(draft.encrypted_payload_json).get("invoice_dispatch")


def _fingerprint(service, invoice):
    lines = service.db.scalars(select(HubFinanceInvoiceLine).where(
        HubFinanceInvoiceLine.invoice_id == invoice.id,
    ).order_by(HubFinanceInvoiceLine.position_index).with_for_update().execution_options(populate_existing=True)).all()
    snapshot = [invoice.customer_id, invoice.contact_id, invoice.encrypted_fields_json,
                [(line.position_index, line.article_id, line.encrypted_fields_json) for line in lines]]
    return sha256(json.dumps(snapshot).encode()).hexdigest()


def _pdf(service, invoice_id, metadata):
    generated = metadata["pdf_kind"] == "generated"
    if generated:
        pdf = service.db.scalar(select(HubFinanceGeneratedPdf).where(
            HubFinanceGeneratedPdf.document_type == "invoices", HubFinanceGeneratedPdf.document_id == invoice_id,
        ))
        if pdf is None or pdf.status != "ready":
            raise HubOperationError("Die Rechnungs-PDF ist noch nicht fertig. Bitte zuerst die PDF erzeugen.")
    else:
        pdf = service.db.scalar(select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == invoice_id))
    if pdf is None or not pdf.is_zugferd or pdf.storage_key != metadata["pdf_key"]:
        raise HubOperationError("Die Rechnungs-PDF hat sich geändert. Bitte den Versand aus der Rechnung neu vorbereiten.")
    storage = FinanceGeneratedPdfStorage(cipher=service.cipher) if generated else FinanceInvoicePdfStorage(cipher=service.cipher)
    try:
        return storage.load(pdf.storage_key)
    except (FinanceGeneratedPdfStorageError, FinanceInvoicePdfStorageError) as exc:
        raise HubOperationError("Die Rechnungs-PDF konnte nicht geladen werden. Bitte zuerst die PDF prüfen oder neu erzeugen.") from exc


def prepare_invoice_email(service, values):
    if set(values) != {"record_id"}:
        raise HubOperationError("Ungültige Rechnungsauswahl.")
    require_actor(service, "emails", "create")
    require_actor(service, "emails", "edit")
    invoice = require_record(service, "invoices", identifier(values["record_id"]))
    mailbox = mailbox_for(service)
    sender = mailbox.scope.mailboxes.default_sender(for_send=True)
    if not sender:
        raise HubOperationError("Es ist kein freigegebenes Absenderpostfach verfügbar.")
    payload, template = HubInvoiceEmailBatchService(db=service.db, cipher=service.cipher, actor=service.actor).prepare_message(invoice.id, sender_email=sender)
    pdf = _pdf(service, invoice.id, payload)
    attachment = HubArtifact(filename=payload["pdf_filename"], content=pdf, content_type="application/pdf")
    draft_service = HubOperationService(db=service.db, cipher=service.cipher, actor=service.actor, input_files=(attachment,))
    result = draft_service.execute("emails.drafts.create", {
        "sender_email": sender, "customer_id": str(invoice.customer_id),
        "recipient_key": payload["recipient_key"], "recipient_email": payload["recipient_email"],
        "recipient_name": payload["recipient_name"],
        "subject": payload["subject"], "content": payload["content"], "template_id": template,
        "context_module": "invoices", "context_record_id": str(invoice.id),
    })
    draft = service.db.get(HubMailboxEmail, result.record_id)
    draft_payload = mailbox._payload(draft.encrypted_payload_json)
    draft_payload["invoice_dispatch"] = {
        "invoice_id": invoice.id, "fingerprint": _fingerprint(service, invoice),
        "pdf_kind": payload["pdf_kind"], "pdf_key": payload["pdf_key"],
        "pdf_sha256": sha256(pdf).hexdigest(),
    }
    draft.encrypted_payload_json = service.cipher.encrypt(json.dumps(draft_payload, ensure_ascii=False))
    service.db.flush()
    return result


SEND_FIELDS = (
    "draft_id", "sender_email", "recipient_email", "recipient_name", "recipient_key",
    "recipient_customer_id", "lead_id", "dunning_id", "subject", "content", "cc_emails",
    "template_id", "context_module", "context_record_id", "retained_attachment_ids",
    "scheduled_at", "reply_to_email_id", "forward_from_email_id",
)


def send_invoice_email(service, values):
    require_actor(service, "emails", "create")
    require_actor(service, "emails", "edit")
    if set(values) - set(SEND_FIELDS):
        raise HubOperationError("Ungültige Versanddaten.")
    mailbox = mailbox_for(service)
    draft_id = identifier(values.get("draft_id", ""))
    mailbox.scope.require(f"unassigned-{draft_id}", "send")
    draft = service.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.id == draft_id)
                              .with_for_update().execution_options(populate_existing=True))
    if draft is None or draft.source != "hub-draft" or draft.mailbox_state != "draft":
        raise HubOperationError("Der Entwurf wurde bereits versendet oder entfernt.")
    metadata = mailbox._payload(draft.encrypted_payload_json).get("invoice_dispatch")
    if not metadata:
        raise HubOperationError("Dies ist kein Rechnungsentwurf.")
    require_editable_invoice_draft(service.db, metadata)
    service.db.scalar(select(HubFinanceInvoice).where(HubFinanceInvoice.id == metadata["invoice_id"])
                      .with_for_update().execution_options(populate_existing=True))
    invoice = require_record(service, "invoices", metadata["invoice_id"])
    if values.get("scheduled_at") or values.get("lead_id") or values.get("dunning_id") or values.get("reply_to_email_id") or values.get("forward_from_email_id"):
        raise HubOperationError("Diese Rechnungs-E-Mail kann nur direkt und manuell versendet werden.")
    if values.get("recipient_customer_id") not in (None, "", str(invoice.customer_id)):
        raise HubOperationError("Der Empfänger gehört zu einem anderen Kunden.")
    if _fingerprint(service, invoice) != metadata["fingerprint"]:
        raise HubOperationError("Die Rechnung wurde inzwischen geändert. Bitte den Versand aus der Rechnung neu vorbereiten.")
    pdf = _pdf(service, invoice.id, metadata)
    if sha256(pdf).hexdigest() != metadata["pdf_sha256"]:
        raise HubOperationError("Die Rechnungs-PDF hat sich geändert. Bitte den Versand neu vorbereiten.")
    _save_draft(service, {**values, "context_module": "invoices", "context_record_id": str(invoice.id),
                         "customer_id": str(invoice.customer_id)}, complete=True)
    context = mailbox.get_draft_compose_context(draft_id=draft_id)
    mailbox.scope.mailboxes.require_sender(context["sender_email"], "send")
    files = mailbox.prepare_draft_delivery_attachments(draft_id=draft_id)
    if not any(sha256(file.content).hexdigest() == metadata["pdf_sha256"] for file in files):
        raise HubOperationError("Die Rechnungs-PDF wurde aus dem Anhang entfernt. Bitte den Versand aus der Rechnung neu vorbereiten.")
    recipient = next((r for r in mailbox.communications.list_recipients(customer_id=invoice.customer_id)
                      if r.email.casefold() == context["recipient_email"].casefold()), None)
    batch = HubInvoiceEmailBatch(review_nonce=token_urlsafe(24), actor=service.actor[:64],
        sender_email=context["sender_email"], template_id=context["template_id"], status="running")
    service.db.add(batch)
    service.db.flush()
    item = HubInvoiceEmailBatchItem(batch_id=batch.id, invoice_id=invoice.id, status="sending",
        encrypted_payload_json=service.cipher.encrypt(json.dumps({"invoice_id": invoice.id, "draft_id": draft_id})))
    service.db.add(item)
    stored = mailbox._payload(draft.encrypted_payload_json)
    stored["invoice_dispatch"]["batch_id"] = batch.id
    draft.encrypted_payload_json = service.cipher.encrypt(json.dumps(stored, ensure_ascii=False))
    service.db.flush()
    batch_id, item_id, invoice_id, customer_id = batch.id, item.id, invoice.id, invoice.customer_id
    # Durable claim before SMTP: concurrent/repeated submits and restarts cannot resend it.
    service.db.commit()
    try:
        if recipient:
            result = mailbox.communications.send_email(customer_id=customer_id, actor=service.actor,
                sender_email=context["sender_email"], recipient_key=recipient.key,
                subject=context["subject"], content=context["content"], cc_emails=context["cc_emails"],
                template_id=context["template_id"], attachments=files)
            success = result.success
        else:
            mailbox.send_direct_email(sender_email=context["sender_email"], recipient_email=context["recipient_email"],
                subject=context["subject"], content=context["content"], cc_emails=context["cc_emails"], attachments=files)
            success = True
        item.status = "sent" if success else "failed"
        item.sent_at = utcnow() if success else None
        item.error = None if success else "Versand fehlgeschlagen. Der Entwurf bleibt erhalten."
        batch.status = "completed"
        if success:
            mailbox.discard_draft(draft_id=draft_id)
        write_audit_log(service.db, site=None, actor=service.actor, source="hub-web", action="send-invoice-email",
                        result="ok" if success else "failed", detail=f"Invoice {invoice_id}, batch {batch_id}; manual dispatch.")
        service.db.commit()
    except Exception:
        service.db.rollback()
        item = service.db.get(HubInvoiceEmailBatchItem, item_id)
        item.status, item.error = "uncertain", "Versandstatus unklar. Bitte vor erneutem Versand prüfen."
        service.db.get(HubInvoiceEmailBatch, batch_id).status = "interrupted"
        service.db.commit()
        logging.getLogger(__name__).warning("Uncertain manual invoice delivery: batch %s", batch_id)
        raise HubOperationError("Versandstatus unklar. Der Entwurf bleibt erhalten. Bitte vor erneutem Versand prüfen.") from None
    if not success:
        raise HubOperationError("Versand fehlgeschlagen. Der Entwurf bleibt zur Korrektur erhalten.")
    return HubOperationResult("Rechnung öffnen", f"/finance/invoices/{invoice_id}?email=success&email_message=E-Mail+wurde+versendet.", invoice_id)


register_operation(HubOperation(key="finance.invoices.email.prepare", module="finance", label="Rechnungs-E-Mail vorbereiten",
    description="Erstellt einen bearbeitbaren Entwurf mit Rechnungsvorlage und vorhandener ZUGFeRD-PDF. Kein Versand.",
    input_guide="record_id ist die Rechnung; Rechte und fertige PDF sind erforderlich.",
    input_fields=lambda: (Field("record_id", "Rechnung", required=True),), preview_fields=(("record_id", "Rechnung"),),
    execute=prepare_invoice_email))
register_operation(HubOperation(key="finance.invoices.email.send", module="finance", label="Rechnungs-E-Mail manuell versenden",
    description="Versendet den manuell bestätigten Rechnungsentwurf und protokolliert den Versandstatus.",
    input_guide="Aktuelle Felder des manuellen E-Mail-Editors einschließlich draft_id.",
    preview_fields=(), execute=send_invoice_email, agent_enabled=False))
