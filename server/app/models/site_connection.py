from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SiteConnection(TimestampMixin, Base):
    __tablename__ = "site_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(Text())
    auth_type: Mapped[str] = mapped_column(String(64))
    encrypted_credentials: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(32), default="active")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    site = relationship("Site", back_populates="connections")

