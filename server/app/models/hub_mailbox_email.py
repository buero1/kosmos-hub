from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

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
