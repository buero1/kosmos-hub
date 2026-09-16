"""Messages skipped by incremental sync but retained for explicit retry."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubMailboxImapSyncFailure(TimestampMixin, Base):
    __tablename__ = "hub_mailbox_imap_sync_failures"
    __table_args__ = (
        UniqueConstraint("mailbox_account_id", "folder", "imap_uid", name="uq_hub_mailbox_sync_failure_uid"),
        Index("ix_hub_mailbox_sync_failures_resolved_at", "resolved_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_account_id: Mapped[int] = mapped_column(
        ForeignKey("hub_mailbox_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    folder: Mapped[str] = mapped_column(String(128), nullable=False)
    imap_uid: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str] = mapped_column(Text(), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
