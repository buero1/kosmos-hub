"""Explicit review and individually tracked bulk delivery of invoice PDFs."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from secrets import token_urlsafe

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import SessionLocal
from app.models.base import utcnow
from app.models.hub_finance_documents import HubFinanceInvoice
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.models.hub_invoice_email_batch import HubInvoiceEmailBatch, HubInvoiceEmailBatchItem
from app.services.hub_invoice_email_delivery import invoice_email_deliveries
from app.services.customer_communications import CustomerCommunicationAttachmentUpload, CustomerCommunicationService
from app.services.audit import write_audit_log
from app.services.finance_generated_pdf_storage import FinanceGeneratedPdfStorage
from app.services.finance_invoice_pdf_storage import FinanceInvoicePdfStorage
from app.services.hub_finance_documents import HubFinanceDocumentService, INVOICE_MODULE
from app.services.hub_mailbox_transport import DEFAULT_HUB_MAILBOX_SENDER_EMAIL, HubMailboxTransportService


logger = logging.getLogger(__name__)
_MAX_BATCH = 1000
_TEMPLATE_NAME = "Rechnungen senden"


class InvoiceEmailBatchError(ValueError):
    """A user-facing preflight or confirmation error."""


@dataclass(frozen=True)
class InvoiceEmailReviewRow:
    invoice_id: int
    number: str
    customer: str
    contact: str
    email: str
    pdf: str
    issue: str
    payload: dict[str, str] | None = None


class HubInvoiceEmailBatchService:
    def __init__(self, *, db: Session, cipher: SecretCipher, actor: str | None = None):
        self.db = db
        self.cipher = cipher
        self.actor = actor
        self.communications = CustomerCommunicationService(
            db=db, cipher=cipher, public_base_url=get_settings().public_base_url, actor=actor,
        )
        self.documents = HubFinanceDocumentService(db=db, cipher=cipher)

    @staticmethod
    def _serializer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(get_settings().app_secret_key, salt="hub-invoice-email-review-v1")

    @staticmethod
    def _ids(raw_ids: object) -> list[int]:
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > _MAX_BATCH:
            raise InvoiceEmailBatchError("Wähle 1 bis 1.000 Rechnungen aus.")
        if any(type(item) is not int or item < 1 for item in raw_ids) or len(set(raw_ids)) != len(raw_ids):
            raise InvoiceEmailBatchError("Die Rechnungsauswahl ist ungültig.")
        return sorted(raw_ids)

    def _template_and_sender(self, sender_email: str | None = None) -> tuple[str, str, str]:
        matches = [item for item in self.communications.list_email_templates() if item.name.casefold() == _TEMPLATE_NAME.casefold()]
        if len(matches) != 1:
            raise InvoiceEmailBatchError(f'Die aktive E-Mail-Vorlage „{_TEMPLATE_NAME}“ muss genau einmal vorhanden sein.')
        sender = sender_email or DEFAULT_HUB_MAILBOX_SENDER_EMAIL
        if not any(item.email.casefold() == sender.casefold() for item in HubMailboxTransportService(db=self.db, cipher=self.cipher, actor=self.actor).list_senders()):
            raise InvoiceEmailBatchError(f"Das Absenderpostfach {sender} ist nicht verifiziert und aktiviert.")
        template = self.communications._stored_email_template(matches[0].id)
        return matches[0].id, sender, sha256(template.encrypted_payload_json.encode("utf-8")).hexdigest()

    def _rows(self, ids: list[int], *, sender_email: str | None = None) -> tuple[list[InvoiceEmailReviewRow], str, str, str]:
        template_id, sender, template_hash = self._template_and_sender(sender_email)
        invoices = {invoice.id: invoice for invoice in self.db.scalars(
            select(HubFinanceInvoice).where(HubFinanceInvoice.id.in_(ids))
        )}
        generated = {pdf.document_id: pdf for pdf in self.db.scalars(
            select(HubFinanceGeneratedPdf).where(
                HubFinanceGeneratedPdf.document_type == "invoices", HubFinanceGeneratedPdf.document_id.in_(ids)
            )
        )}
        imported = {pdf.invoice_id: pdf for pdf in self.db.scalars(
            select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id.in_(ids))
        )}
        rows: list[InvoiceEmailReviewRow] = []
        for invoice_id in ids:
            invoice = invoices.get(invoice_id)
            if invoice is None:
                rows.append(InvoiceEmailReviewRow(invoice_id, "-", "-", "-", "-", "-", "Rechnung nicht mehr vorhanden."))
                continue
            detail = self.documents.get_detail(module=INVOICE_MODULE, document_id=invoice_id)
            assert detail is not None
            fields = {field.key: field.value for field in detail.fields}
            customer = detail.document.customer
            contact_id = detail.document.contact_id
            recipients = self.communications.list_contact_recipients(customer_id=customer.id) if customer else ()
            recipient = next((item for item in recipients if item.key.startswith(f"contact:{contact_id}:")), None)
            pdf = generated.get(invoice_id)
            original = imported.get(invoice_id)
            pdf_kind = "generated" if pdf and pdf.status == "ready" and pdf.is_zugferd and pdf.storage_key else ""
            if not pdf_kind and original and original.is_zugferd:
                pdf_kind = "imported"
            selected_pdf = pdf if pdf_kind == "generated" else original if pdf_kind == "imported" else None
            issue = ""
            if detail.status == "Storniert":
                issue = "Stornierte Rechnungen werden nicht versendet."
            elif not fields.get("due_date"):
                issue = "Fälligkeitsdatum fehlt."
            elif not customer:
                issue = "Kunde fehlt."
            elif not contact_id or not detail.document.contact or detail.document.contact.customer_id != customer.id:
                issue = "Ansprechpartner fehlt oder gehört nicht zum Kunden."
            elif not recipient:
                issue = "Keine E-Mail-Adresse beim Ansprechpartner."
            elif not selected_pdf or not selected_pdf.storage_key:
                issue = "Keine fertige ZUGFeRD-PDF vorhanden."
            elif (selected_pdf.byte_size or 0) > 50 * 1024 * 1024:
                issue = "PDF-Anhang ist größer als 50 MB."
            payload = None
            if not issue and recipient and selected_pdf and customer:
                invoice_values = {
                    "Invoice.Number": detail.identifier,
                    "Invoice.Title": "Rechnung",
                    "Invoice.Status": detail.status,
                    "Invoice.Date": fields.get("invoice_date", ""),
                    "Invoice.DueDate": fields.get("due_date", ""),
                    "Invoice.PaymentTerms": fields.get("payment_terms", ""),
                    "Invoice.NetTotal": detail.totals.subtotal_net_display,
                    "Invoice.TaxTotal": detail.totals.tax_total_display,
                    "Invoice.GrossTotal": detail.totals.total_gross_display,
                }
                rendered = self.communications.render_invoice_email_template(
                    template_id=template_id, customer_id=customer.id,
                    recipient=recipient, invoice_values=invoice_values,
                )
                if rendered.unresolved_placeholders:
                    issue = "Unbekannte Platzhalter: " + ", ".join(rendered.unresolved_placeholders[:4])
                elif not rendered.subject.strip() or not rendered.content.strip():
                    issue = "Betreff oder Inhalt der Vorlage ist leer."
                else:
                    payload = {
                        "invoice_id": str(invoice_id),
                        "customer_id": str(customer.id), "contact_id": str(contact_id),
                        "recipient_key": recipient.key, "recipient_email": recipient.email,
                        "subject": rendered.subject, "content": rendered.content,
                        "pdf_kind": pdf_kind, "pdf_key": selected_pdf.storage_key,
                        "pdf_filename": selected_pdf.filename or f"{detail.identifier}.pdf",
                    }
            rows.append(InvoiceEmailReviewRow(
                invoice_id=invoice_id, number=detail.identifier,
                customer=customer.name if customer else "-", contact=detail.contact_name or "-",
                email=recipient.email if recipient else "-",
                pdf=(selected_pdf.filename or "-") if selected_pdf else "-", issue=issue, payload=payload,
            ))
        return rows, template_id, sender, template_hash

    def prepare_message(self, invoice_id: int, *, sender_email: str) -> tuple[dict[str, str], str]:
        rows, template_id, _sender, _hash = self._rows([invoice_id], sender_email=sender_email)
        row = rows[0]
        if row.issue or row.payload is None:
            raise InvoiceEmailBatchError(row.issue or "Die Rechnung konnte nicht vorbereitet werden.")
        return {**row.payload, "recipient_name": row.contact}, template_id

    @staticmethod
    def _digest(rows: list[InvoiceEmailReviewRow], template_hash: str) -> str:
        entries = [
            (row.invoice_id, row.number, row.customer, row.contact, row.email, row.pdf, row.issue, row.payload)
            for row in rows
        ]
        return sha256(json.dumps([template_hash, entries], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def review(self, raw_ids: object, *, actor: str) -> dict[str, object]:
        ids = self._ids(raw_ids)
        rows, template_id, sender, template_hash = self._rows(ids)
        eligible = all(not row.issue for row in rows)
        token = self._serializer().dumps({
            "ids": ids, "digest": self._digest(rows, template_hash),
            "nonce": token_urlsafe(24), "actor": actor[:64],
        }) if eligible else ""
        return {
            "template": _TEMPLATE_NAME, "sender": sender, "review_token": token,
            "rows": [{"invoice_id": row.invoice_id, "number": row.number, "customer": row.customer,
                      "contact": row.contact, "email": row.email, "pdf": row.pdf, "issue": row.issue} for row in rows],
        }

    def confirm(self, *, token: str, actor: str) -> HubInvoiceEmailBatch:
        try:
            signed = self._serializer().loads(token, max_age=600)
        except (BadSignature, SignatureExpired) as exc:
            raise InvoiceEmailBatchError("Die Bestätigung ist abgelaufen. Bitte die Auswahl erneut prüfen.") from exc
        if not isinstance(signed, dict) or not isinstance(signed.get("nonce"), str) or signed.get("actor") != actor[:64]:
            raise InvoiceEmailBatchError("Die Bestätigung ist ungültig.")
        ids = self._ids(signed.get("ids"))
        existing = self.db.scalar(select(HubInvoiceEmailBatch).where(HubInvoiceEmailBatch.review_nonce == signed["nonce"]))
        if existing:
            return existing
        rows, template_id, sender, template_hash = self._rows(ids)
        if any(row.issue for row in rows) or self._digest(rows, template_hash) != signed.get("digest"):
            raise InvoiceEmailBatchError("Rechnung, Empfänger, Vorlage oder PDF haben sich geändert. Bitte erneut prüfen.")
        batch = HubInvoiceEmailBatch(
            review_nonce=signed["nonce"], actor=actor[:64], sender_email=sender,
            template_id=template_id, status="queued",
        )
        self.db.add(batch)
        self.db.flush()
        for row in rows:
            assert row.payload is not None
            self.db.add(HubInvoiceEmailBatchItem(
                batch_id=batch.id, invoice_id=row.invoice_id, status="queued",
                encrypted_payload_json=self.cipher.encrypt(json.dumps(row.payload, ensure_ascii=False)),
            ))
        self.db.flush()
        return batch

    def status(self, *, batch_id: int, actor: str) -> dict[str, object]:
        batch = self.db.scalar(select(HubInvoiceEmailBatch).options(selectinload(HubInvoiceEmailBatch.items)).where(
            HubInvoiceEmailBatch.id == batch_id, HubInvoiceEmailBatch.actor == actor[:64],
        ))
        if batch is None:
            raise InvoiceEmailBatchError("Versandrunde nicht gefunden.")
        deliveries = invoice_email_deliveries(self.db, [item.invoice_id for item in batch.items if item.invoice_id is not None])
        return {"status": batch.status, "items": [
            {"invoice_id": item.invoice_id or int(json.loads(self.cipher.decrypt(item.encrypted_payload_json))["invoice_id"]),
             "status": item.status, "error": item.error or "",
             "delivery_status": deliveries[item.invoice_id].label if item.invoice_id in deliveries else "-",
             "delivery_sent_at": deliveries[item.invoice_id].sent_at_display if item.invoice_id in deliveries else "-"}
            for item in batch.items
        ]}


def run_invoice_email_batch(batch_id: int) -> None:
    """Commit a 'sending' marker first; never blindly retry an uncertain SMTP outcome."""
    with SessionLocal() as db:
        cipher = get_secret_cipher()
        claimed = db.execute(update(HubInvoiceEmailBatch).where(
            HubInvoiceEmailBatch.id == batch_id, HubInvoiceEmailBatch.status == "queued",
        ).values(status="running"))
        if claimed.rowcount != 1:
            db.rollback()
            return
        db.commit()
        batch = db.get(HubInvoiceEmailBatch, batch_id)
        item_ids = db.scalars(select(HubInvoiceEmailBatchItem.id).where(
            HubInvoiceEmailBatchItem.batch_id == batch_id,
            HubInvoiceEmailBatchItem.status == "queued",
        ).order_by(HubInvoiceEmailBatchItem.id)).all()
        for item_id in item_ids:
            item = db.get(HubInvoiceEmailBatchItem, item_id)
            if item is None or item.status != "queued":
                continue
            item.status = "sending"
            db.commit()
            try:
                payload = json.loads(cipher.decrypt(item.encrypted_payload_json))
                invoice = db.get(HubFinanceInvoice, item.invoice_id)
                if invoice is None or invoice.customer_id != int(payload["customer_id"]) or invoice.contact_id != int(payload["contact_id"]):
                    raise InvoiceEmailBatchError("Rechnung oder Kontakt hat sich geändert.")
                communications = CustomerCommunicationService(
                    db=db, cipher=cipher, public_base_url=get_settings().public_base_url,
                )
                recipients = communications.list_contact_recipients(customer_id=invoice.customer_id)
                if not any(recipient.key == payload["recipient_key"] and recipient.email == payload["recipient_email"] for recipient in recipients):
                    raise InvoiceEmailBatchError("Die Empfängeradresse hat sich geändert.")
                if payload["pdf_kind"] == "generated":
                    pdf = db.scalar(select(HubFinanceGeneratedPdf).where(
                        HubFinanceGeneratedPdf.document_type == "invoices", HubFinanceGeneratedPdf.document_id == item.invoice_id,
                    ))
                    if pdf is None or pdf.status != "ready" or not pdf.is_zugferd or pdf.storage_key != payload["pdf_key"]:
                        raise InvoiceEmailBatchError("Die ZUGFeRD-PDF hat sich geändert.")
                    content = FinanceGeneratedPdfStorage(cipher=cipher).load(pdf.storage_key)
                else:
                    pdf = db.scalar(select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == item.invoice_id))
                    if pdf is None or not pdf.is_zugferd or pdf.storage_key != payload["pdf_key"]:
                        raise InvoiceEmailBatchError("Die ZUGFeRD-PDF hat sich geändert.")
                    content = FinanceInvoicePdfStorage(cipher=cipher).load(pdf.storage_key)
                result = communications.send_email(
                    customer_id=invoice.customer_id, actor=batch.actor, sender_email=batch.sender_email,
                    recipient_key=payload["recipient_key"], subject=payload["subject"], content=payload["content"],
                    template_id=batch.template_id, attachments=(CustomerCommunicationAttachmentUpload(
                        filename=payload["pdf_filename"], content=content, content_type="application/pdf",
                    ),),
                )
                item.status = "sent" if result.success else "failed"
                item.sent_at = utcnow() if result.success else None
                item.error = None if result.success else "Versand fehlgeschlagen; Details im E-Mail-Protokoll des Kunden prüfen."
                write_audit_log(
                    db, site=None, actor=batch.actor, source="hub-worker", action="send-invoice-email",
                    result="ok" if result.success else "failed",
                    detail=f"Invoice {item.invoice_id}, batch {batch_id}; recipients and content omitted.",
                )
                db.commit()
            except Exception:
                logger.exception("Invoice email batch %s item %s has an uncertain outcome.", batch_id, item_id)
                db.rollback()
                item = db.get(HubInvoiceEmailBatchItem, item_id)
                item.status = "uncertain"
                item.error = "Versandstatus unklar; bitte vor erneutem Versand prüfen."
                write_audit_log(
                    db, site=None, actor=batch.actor, source="hub-worker", action="send-invoice-email",
                    result="uncertain", detail=f"Invoice {item.invoice_id}, batch {batch_id}; SMTP result requires manual review.",
                )
                db.commit()
        batch = db.get(HubInvoiceEmailBatch, batch_id)
        batch.status = "completed"
        db.commit()


def resume_queued_invoice_email_batches() -> None:
    """Recover only rounds that never started; ambiguous SMTP attempts are not retried."""
    with SessionLocal() as db:
        interrupted = db.scalars(select(HubInvoiceEmailBatch.id).where(
            HubInvoiceEmailBatch.status == "running",
        )).all()
        if interrupted:
            db.execute(update(HubInvoiceEmailBatch).where(
                HubInvoiceEmailBatch.id.in_(interrupted),
            ).values(status="interrupted"))
            db.execute(update(HubInvoiceEmailBatchItem).where(
                HubInvoiceEmailBatchItem.batch_id.in_(interrupted), HubInvoiceEmailBatchItem.status == "sending",
            ).values(status="uncertain", error="Dienst wurde während des Versands beendet; Zustellung bitte prüfen."))
            db.execute(update(HubInvoiceEmailBatchItem).where(
                HubInvoiceEmailBatchItem.batch_id.in_(interrupted), HubInvoiceEmailBatchItem.status == "queued",
            ).values(status="failed", error="Nicht versendet: Versandrunde wurde unterbrochen."))
            db.commit()
        ids = db.scalars(select(HubInvoiceEmailBatch.id).where(
            HubInvoiceEmailBatch.status == "queued",
        ).order_by(HubInvoiceEmailBatch.id)).all()
    for batch_id in ids:
        run_invoice_email_batch(batch_id)
