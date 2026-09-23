from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubScheduledEmail(TimestampMixin, Base):
    """A durable, encrypted email delivery scheduled by a Hub user."""

    __tablename__ = "hub_scheduled_emails"
    __table_args__ = (
        Index("ix_hub_scheduled_emails_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_hub_scheduled_emails_customer_status", "customer_id", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("hub_leads.id", ondelete="SET NULL"), nullable=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    dunning_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_finance_dunnings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    customer_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_zoho_emails.id", ondelete="SET NULL"),
        nullable=True,
    )
    mailbox_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_mailbox_emails.id", ondelete="SET NULL"),
        nullable=True,
    )
    creator_username: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_payload_json: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT, "mysql"),
        nullable=False,
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    attempt_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)

    attachments = relationship(
        "HubScheduledEmailAttachment",
        back_populates="scheduled_email",
        cascade="all, delete-orphan",
    )


class HubScheduledEmailAttachment(TimestampMixin, Base):
    """Metadata for an encrypted attachment waiting for scheduled delivery."""

    __tablename__ = "hub_scheduled_email_attachments"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_hub_scheduled_email_attachments_storage_key"),
        Index("ix_hub_scheduled_email_attachments_email_id", "scheduled_email_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scheduled_email_id: Mapped[int] = mapped_column(
        ForeignKey("hub_scheduled_emails.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(96), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    byte_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    stored_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)

    scheduled_email = relationship("HubScheduledEmail", back_populates="attachments")
