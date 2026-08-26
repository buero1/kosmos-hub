from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class FleetRefreshSettings(TimestampMixin, Base):
    """Singleton configuration for safe, user-controlled fleet refreshes."""

    __tablename__ = "fleet_refresh_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    site_status_max_age_minutes: Mapped[int] = mapped_column(Integer, default=15)
    official_version_max_age_hours: Mapped[int] = mapped_column(Integer, default=24)
    max_parallel_site_checks: Mapped[int] = mapped_column(Integer, default=5)
    max_parallel_direct_updates: Mapped[int] = mapped_column(Integer, default=5)
    configured_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id"), nullable=True)
    configured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
