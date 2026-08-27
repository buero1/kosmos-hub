from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    zoho_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zoho_status: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    is_visible: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True, index=True)
    website_domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    encrypted_profile_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sites = relationship("Site", back_populates="customer")
