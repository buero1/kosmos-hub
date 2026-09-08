"""High-water marks for the automatic Mittwald mailbox sync."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubMailboxImapSyncState(TimestampMixin, Base):
    """Stores the newest imported IMAP UID for one mailbox folder."""

    __tablename__ = "hub_mailbox_imap_sync_states"
    __table_args__ = (
        UniqueConstraint("mailbox_account_id", "folder", name="uq_hub_mailbox_imap_sync_state_folder"),
        Index("ix_hub_mailbox_imap_sync_states_last_synced_at", "last_synced_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_account_id: Mapped[int] = mapped_column(
        ForeignKey("hub_mailbox_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    folder: Mapped[str] = mapped_column(String(128), nullable=False)
    last_imap_uid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
