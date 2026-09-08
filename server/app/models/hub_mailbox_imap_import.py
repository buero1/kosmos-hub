"""Durable work queue for a one-time or later incremental Mittwald IMAP import."""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubMailboxImapImport(TimestampMixin, Base):
    """One bounded selection of messages from configured Mittwald mailboxes."""

    __tablename__ = "hub_mailbox_imap_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    since_date: Mapped[date] = mapped_column(Date(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    total_messages: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    processed_messages: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    imported_messages: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    skipped_messages: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_messages: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    stored_attachments: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    stored_bytes: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    items = relationship("HubMailboxImapImportItem", back_populates="mailbox_import", cascade="all, delete-orphan")


class HubMailboxImapImportItem(TimestampMixin, Base):
    """One immutable IMAP UID selected before the import worker begins fetching content."""

    __tablename__ = "hub_mailbox_imap_import_items"
    __table_args__ = (
        UniqueConstraint("mailbox_import_id", "mailbox_account_id", "folder", "imap_uid", name="uq_hub_mailbox_imap_import_item"),
        Index("ix_hub_mailbox_imap_import_items_status", "mailbox_import_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_import_id: Mapped[int] = mapped_column(
        ForeignKey("hub_mailbox_imap_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mailbox_account_id: Mapped[int] = mapped_column(
        ForeignKey("hub_mailbox_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    folder: Mapped[str] = mapped_column(String(128), nullable=False)
    imap_uid: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    mailbox_import = relationship("HubMailboxImapImport", back_populates="items")
