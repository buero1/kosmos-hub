from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubLeadEmail(TimestampMixin, Base):
    """An encrypted Zoho email imported specifically for one Hub Lead."""

    __tablename__ = "hub_lead_emails"
    __table_args__ = (
        UniqueConstraint("lead_id", "zoho_message_id", name="uq_hub_lead_emails_lead_id_zoho_message_id"),
        Index("ix_hub_lead_emails_lead_id_sent_at", "lead_id", "zoho_sent_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("hub_leads.id", ondelete="CASCADE"), nullable=False, index=True)
    zoho_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    zoho_module: Mapped[str] = mapped_column(String(32), nullable=False, default="Leads")
    zoho_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    encrypted_payload_json: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False)
    encrypted_header_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    lead = relationship("HubLead", back_populates="zoho_emails")
