"""Reviewed, individually tracked invoice email deliveries."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubInvoiceEmailBatch(TimestampMixin, Base):
    __tablename__ = "hub_invoice_email_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    review_nonce: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    sender_email: Mapped[str] = mapped_column(String(255), nullable=False)
    template_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    items = relationship("HubInvoiceEmailBatchItem", back_populates="batch", cascade="all, delete-orphan", order_by="HubInvoiceEmailBatchItem.id")


class HubInvoiceEmailBatchItem(TimestampMixin, Base):
    __tablename__ = "hub_invoice_email_batch_items"
    __table_args__ = (UniqueConstraint("batch_id", "invoice_id", name="uq_hub_invoice_email_batch_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("hub_invoice_email_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    encrypted_payload_json: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False)
    error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch = relationship("HubInvoiceEmailBatch", back_populates="items")
