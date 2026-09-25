"""One durable conversion per Lead, retained even if a target is deleted."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class HubLeadConversion(Base):
    __tablename__ = "hub_lead_conversions"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("hub_leads.id", ondelete="SET NULL"), unique=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), unique=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), unique=True)
    converted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id", ondelete="SET NULL"))
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_origin: Mapped[str] = mapped_column(String(24), nullable=False)
