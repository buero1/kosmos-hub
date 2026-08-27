from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ZohoConnection(TimestampMixin, Base):
    """The single Zoho CRM connection configured for this Hub."""

    __tablename__ = "zoho_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    data_center: Mapped[str] = mapped_column(String(16), default="eu")
    api_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_client_id: Mapped[str] = mapped_column(Text())
    encrypted_client_secret: Mapped[str] = mapped_column(Text())
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text(), nullable=True)
    scopes: Mapped[str] = mapped_column(String(512))
    field_map_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_metadata_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    configured_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id"), nullable=True)
