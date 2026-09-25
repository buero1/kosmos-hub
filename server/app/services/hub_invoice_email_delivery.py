"""Read-only invoice delivery summaries from the existing dispatch journal."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.timezones import format_berlin_time_short
from app.models.hub_invoice_email_batch import HubInvoiceEmailBatchItem


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
            "failed": "Fehlgeschlagen",
            "uncertain": "Status unklar",
        }.get(self.status, "Status unklar")

    @property
    def sent_at_display(self) -> str:
        return format_berlin_time_short(self.sent_at)


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
        func.max(case((item.status == "sent", item.sent_at))).label("sent_at"),
    ).where(item.invoice_id.in_(invoice_ids)).group_by(item.invoice_id).subquery()
    rows = db.execute(select(
        summary.c.invoice_id, item.status, summary.c.sent_at,
    ).join(item, item.id == summary.c.latest_id)).all()
    result = dict.fromkeys(invoice_ids, InvoiceEmailDelivery())
    result.update({row.invoice_id: InvoiceEmailDelivery(row.status, row.sent_at) for row in rows})
    return result
