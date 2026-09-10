from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubLeadNote(TimestampMixin, Base):
    """A Lead note imported from Zoho and stored only in the Hub."""

    __tablename__ = "hub_lead_notes"
    __table_args__ = (
        UniqueConstraint("lead_id", "zoho_note_id", name="uq_hub_lead_notes_lead_id_zoho_note_id"),
        Index("ix_hub_lead_notes_lead_created_at", "lead_id", "zoho_created_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("hub_leads.id", ondelete="CASCADE"), nullable=False, index=True)
    zoho_note_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    encrypted_payload_json: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    zoho_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    lead = relationship("HubLead", back_populates="zoho_notes")
