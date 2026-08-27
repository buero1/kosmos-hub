from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SiteUserSnapshot(TimestampMixin, Base):
    """An encrypted, point-in-time inventory of WordPress users for one site."""

    __tablename__ = "site_user_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer(), ForeignKey("sites.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    available: Mapped[bool] = mapped_column(Boolean(), default=True)
    user_count: Mapped[int] = mapped_column(Integer(), default=0)
    encrypted_users_json: Mapped[str] = mapped_column(Text())
    message: Mapped[str | None] = mapped_column(String(512), nullable=True)

    site = relationship("Site", back_populates="user_snapshots")
