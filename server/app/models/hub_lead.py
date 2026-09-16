from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubLead(TimestampMixin, Base):
    """A locally stored Lead, optionally identified by the later one-time Zoho import."""

    __tablename__ = "hub_leads"
    __table_args__ = (UniqueConstraint("source_system", "source_external_id", name="uq_hub_leads_source_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    zoho_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    source_system: Mapped[str | None] = mapped_column(String(96), nullable=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_profile_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    zoho_emails = relationship("HubLeadEmail", back_populates="lead", cascade="all, delete-orphan")
    zoho_notes = relationship("HubLeadNote", back_populates="lead", cascade="all, delete-orphan")
