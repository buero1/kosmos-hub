from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ZohoBooksConnection(TimestampMixin, Base):
    """The single read-only Zoho Books connection configured for this Hub."""

    __tablename__ = "zoho_books_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    data_center: Mapped[str] = mapped_column(String(16), default="eu")
    api_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_client_id: Mapped[str] = mapped_column(Text())
    encrypted_client_secret: Mapped[str] = mapped_column(Text())
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text(), nullable=True)
    scopes: Mapped[str] = mapped_column(String(1024))
    organization_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    organization_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    available_organizations_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_organization_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    configured_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id"), nullable=True)
