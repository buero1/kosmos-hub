from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PluginInstallationPackage(TimestampMixin, Base):
    """A Hub-verified package retained while a queued installation needs it."""

    __tablename__ = "plugin_installation_packages"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    original_filename: Mapped[str] = mapped_column(String(255))
    plugin_file: Mapped[str] = mapped_column(String(255), index=True)
    plugin_name: Mapped[str] = mapped_column(String(255))
    plugin_version: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    package_bytes: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB, "mysql"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
