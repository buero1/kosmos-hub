from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubMailboxEmail(TimestampMixin, Base):
    """Inbound Zoho workflow mail that is not yet associated with a Hub customer."""

    __tablename__ = "hub_mailbox_emails"
    __table_args__ = (
        Index("ix_hub_mailbox_emails_mailbox_state_direction_received_at", "mailbox_state", "direction", "received_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="zoho-workflow")
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="inbound", index=True)
    is_unread: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True, index=True)
    mailbox_state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    encrypted_payload_json: Mapped[str] = mapped_column(Text(), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    stored_attachments = relationship("HubMailboxAttachment", back_populates="email", cascade="all, delete-orphan")


class HubMailboxAttachment(TimestampMixin, Base):
    """Metadata for an encrypted attachment belonging to an unassigned mailbox email."""

    __tablename__ = "hub_mailbox_attachments"
    __table_args__ = (
        UniqueConstraint("email_id", "source_attachment_id", name="uq_hub_mailbox_attachments_email_source"),
        UniqueConstraint("storage_key", name="uq_hub_mailbox_attachments_storage_key"),
        Index("ix_hub_mailbox_attachments_email_id", "email_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("hub_mailbox_emails.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="mittwald-imap")
    source_attachment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(96), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    byte_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    email = relationship("HubMailboxEmail", back_populates="stored_attachments")
