from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ZohoNotificationChannel(TimestampMixin, Base):
    """A short-lived Zoho notification channel used to test CRM callbacks."""

    __tablename__ = "zoho_notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    module: Mapped[str] = mapped_column(String(64), unique=True)
    channel_id: Mapped[str] = mapped_column(String(32), unique=True)
    encrypted_token: Mapped[str] = mapped_column(Text())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    encrypted_last_payload_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
