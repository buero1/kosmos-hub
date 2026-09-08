from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubDesktopDevice(TimestampMixin, Base):
    """A Windows notifier installation paired with one Hub user."""

    __tablename__ = "hub_desktop_devices"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_hub_desktop_devices_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("hub_users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
