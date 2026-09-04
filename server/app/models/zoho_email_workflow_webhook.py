from datetime import datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ZohoEmailWorkflowWebhook(TimestampMixin, Base):
    """Credential and delivery status for a manually configured Zoho email webhook."""

    __tablename__ = "zoho_email_workflow_webhooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    encrypted_token: Mapped[str] = mapped_column(Text(), nullable=False)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    encrypted_last_payload_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
