from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PluginOfficialVersion(TimestampMixin, Base):
    """Cached official version evidence for one WordPress plugin file."""

    __tablename__ = "plugin_official_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    plugin_file: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    official_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(128))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
