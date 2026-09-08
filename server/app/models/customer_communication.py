from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGBLOB, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CustomerZohoNote(TimestampMixin, Base):
    """A customer note mirrored from Zoho or created in the Hub."""

    __tablename__ = "customer_zoho_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    zoho_note_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="synced")
    encrypted_payload_json: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zoho_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    customer = relationship("Customer", back_populates="zoho_notes")


class CustomerZohoEmail(TimestampMixin, Base):
    """A customer email header and, when requested, its encrypted message body."""

    __tablename__ = "customer_zoho_emails"
    __table_args__ = (
        UniqueConstraint("customer_id", "zoho_message_id", name="uq_customer_zoho_emails_customer_id_zoho_message_id"),
        Index("ix_customer_zoho_emails_zoho_message_id", "zoho_message_id"),
        Index("ix_customer_zoho_emails_direction_is_unread_message_id", "direction", "is_unread", "zoho_message_id"),
        Index("ix_customer_zoho_emails_direction_sent_at", "direction", "zoho_sent_at", "id"),
        Index("ix_customer_zoho_emails_mailbox_state_direction_sent_at", "mailbox_state", "direction", "zoho_sent_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    zoho_message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    zoho_module: Mapped[str | None] = mapped_column(String(32), nullable=True)
    zoho_record_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    is_unread: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    mailbox_state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="synced")
    encrypted_payload_json: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False)
    encrypted_header_json: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zoho_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    customer = relationship("Customer", back_populates="zoho_emails")
    cached_images = relationship("CustomerZohoEmailImage", back_populates="email", cascade="all, delete-orphan")
    stored_attachments = relationship("CustomerEmailAttachment", back_populates="email", cascade="all, delete-orphan")


class CustomerEmailAttachment(TimestampMixin, Base):
    """Metadata for an encrypted email attachment kept on the Hub server."""

    __tablename__ = "customer_email_attachments"
    __table_args__ = (
        UniqueConstraint("email_id", "source_attachment_id", name="uq_customer_email_attachments_email_source"),
        UniqueConstraint("storage_key", name="uq_customer_email_attachments_storage_key"),
        Index("ix_customer_email_attachments_email_id", "email_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("customer_zoho_emails.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="zoho")
    source_attachment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(96), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    byte_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    email = relationship("CustomerZohoEmail", back_populates="stored_attachments")


class CustomerZohoEmailImage(TimestampMixin, Base):
    """An encrypted, short-lived copy of an external image from an email preview."""

    __tablename__ = "customer_zoho_email_images"
    __table_args__ = (
        UniqueConstraint("email_id", "source_url_hash", name="uq_customer_zoho_email_images_email_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("customer_zoho_emails.id", ondelete="CASCADE"), nullable=False, index=True)
    source_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_image_bytes: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB, "mysql"), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    email = relationship("CustomerZohoEmail", back_populates="cached_images")
