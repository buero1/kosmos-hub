"""Invoice delivery summaries and history from the dispatch journal."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import InvalidToken
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.core.timezones import format_berlin_time_short
from app.models.hub_invoice_email_batch import HubInvoiceEmailBatch, HubInvoiceEmailBatchItem


@dataclass(frozen=True)
class InvoiceEmailDelivery:
    status: str = "not_sent"
    sent_at: datetime | None = None

    @property
    def label(self) -> str:
        return {
            "not_sent": "Noch nicht versendet",
            "queued": "Wartet",
            "sending": "Wird versendet",
            "sent": "Versendet",
            "marked_sent": "Als versandt markiert",
            "failed": "Fehlgeschlagen",
            "uncertain": "Status unklar",
        }.get(self.status, "Status unklar")

    @property
    def sent_at_display(self) -> str:
        return format_berlin_time_short(self.sent_at)

    @property
    def is_confirmed(self) -> bool:
        return self.sent_at is not None


@dataclass(frozen=True)
class InvoiceHistoryEntry:
    title: str
    occurred_at: datetime
    detail: str = ""

    @property
    def occurred_at_display(self) -> str:
        return format_berlin_time_short(self.occurred_at)


def invoice_email_deliveries(db: Session, invoice_ids: list[int]) -> dict[int, InvoiceEmailDelivery]:
    """One bounded query, without decrypting recipients or loading email bodies.

    The latest confirmed attempt determines status. Only explicit UTC send times
    count: legacy server-local updated_at values are not proof of a send time.
    A failed resend must not erase the last successful send time.
    """
    if not invoice_ids:
        return {}
    item = HubInvoiceEmailBatchItem
    summary = select(
        item.invoice_id,
        func.max(item.id).label("latest_id"),
        func.max(case((item.status.in_(("sent", "marked_sent")), item.sent_at))).label("sent_at"),
    ).where(item.invoice_id.in_(invoice_ids)).group_by(item.invoice_id).subquery()
    rows = db.execute(select(
        summary.c.invoice_id, item.status, summary.c.sent_at,
    ).join(item, item.id == summary.c.latest_id)).all()
    result = dict.fromkeys(invoice_ids, InvoiceEmailDelivery())
    result.update({row.invoice_id: InvoiceEmailDelivery(row.status, row.sent_at) for row in rows})
    return result


def invoice_history(
    db: Session,
    cipher: SecretCipher,
    *,
    invoice_id: int,
    created_at: datetime,
) -> tuple[InvoiceHistoryEntry, ...]:
    """Return durable invoice events newest first without exposing message content."""
    entries = [InvoiceHistoryEntry("Rechnung im Hub erstellt", created_at)]
    rows = db.execute(
        select(HubInvoiceEmailBatchItem, HubInvoiceEmailBatch)
        .join(HubInvoiceEmailBatch, HubInvoiceEmailBatch.id == HubInvoiceEmailBatchItem.batch_id)
        .where(
            HubInvoiceEmailBatchItem.invoice_id == invoice_id,
            HubInvoiceEmailBatchItem.status.in_(("sent", "marked_sent", "failed", "uncertain")),
        )
    ).all()
    titles = {
        "sent": "Per E-Mail versendet",
        "marked_sent": "Als versendet markiert",
        "failed": "E-Mail-Versand fehlgeschlagen",
        "uncertain": "Versandstatus unklar",
    }
    for item, batch in rows:
        try:
            payload = json.loads(cipher.decrypt(item.encrypted_payload_json))
        except (InvalidToken, TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        recipient_email = str(payload.get("recipient_email") or "").strip()
        recipient_name = str(payload.get("recipient_name") or "").strip()
        recipient = (
            f"{recipient_name} <{recipient_email}>"
            if recipient_name and recipient_name.casefold() != recipient_email.casefold()
            else recipient_email
        )
        details = []
        if recipient:
            details.append(f"An {recipient}")
        if batch.actor:
            details.append(f"Ausgeführt von {batch.actor}")
        occurred_at = item.sent_at or item.updated_at or item.created_at
        entries.append(InvoiceHistoryEntry(titles[item.status], occurred_at, " · ".join(details)))

    def sort_key(entry: InvoiceHistoryEntry) -> datetime:
        value = entry.occurred_at
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    return tuple(sorted(entries, key=sort_key, reverse=True))
