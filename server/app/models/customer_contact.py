from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CustomerContact(TimestampMixin, Base):
    """A read-only Zoho Contact associated with one imported customer."""

    __tablename__ = "customer_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    zoho_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    encrypted_profile_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer", back_populates="contacts")
