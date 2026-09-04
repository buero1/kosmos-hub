from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ZohoEmailWorkflowDelivery(TimestampMixin, Base):
    """A durable inbound webhook delivery waiting for Zoho communication sync."""

    __tablename__ = "zoho_email_workflow_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    encrypted_payload_json: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
