from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class FleetRefreshSettings(TimestampMixin, Base):
    """Singleton configuration for safe, user-controlled fleet refreshes."""

    __tablename__ = "fleet_refresh_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    site_status_max_age_minutes: Mapped[int] = mapped_column(Integer, default=15)
    max_parallel_site_checks: Mapped[int] = mapped_column(Integer, default=5)
    max_parallel_direct_updates: Mapped[int] = mapped_column(Integer, default=5)
    auto_refresh_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_refresh_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    auto_refresh_time: Mapped[str] = mapped_column(String(5), default="03:00")
    auto_refresh_next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    configured_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id"), nullable=True)
    configured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
