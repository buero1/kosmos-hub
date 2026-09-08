from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubMailboxAccount(TimestampMixin, Base):
    """A Mittwald mailbox whose secret is encrypted before it reaches the database."""

    __tablename__ = "hub_mailbox_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_address: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    username: Mapped[str] = mapped_column(String(320))
    encrypted_password: Mapped[str] = mapped_column(Text())
    imap_host: Mapped[str] = mapped_column(String(255), default="mail.agenturserver.de")
    imap_port: Mapped[int] = mapped_column(default=993)
    imap_root_folder: Mapped[str] = mapped_column(String(64), default="INBOX")
    smtp_host: Mapped[str] = mapped_column(String(255), default="mail.agenturserver.de")
    smtp_port: Mapped[int] = mapped_column(default=465)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    configured_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
