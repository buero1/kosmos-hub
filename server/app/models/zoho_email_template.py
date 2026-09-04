from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ZohoEmailTemplate(TimestampMixin, Base):
    """A locally encrypted, one-way copy of a Zoho email template."""

    __tablename__ = "zoho_email_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    zoho_template_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False, default="Accounts")
    encrypted_payload_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True, index=True)
