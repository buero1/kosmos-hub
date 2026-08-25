from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SiteBackupSnapshot(TimestampMixin, Base):
    __tablename__ = "site_backup_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    provider_installed: Mapped[bool] = mapped_column(Boolean())
    provider_active: Mapped[bool] = mapped_column(Boolean())
    backup_available: Mapped[bool] = mapped_column(Boolean())
    backup_complete: Mapped[bool] = mapped_column(Boolean())
    backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backup_count: Mapped[int] = mapped_column(Integer(), default=0)
    components_json: Mapped[list] = mapped_column(JSON)
    summary_json: Mapped[dict] = mapped_column(JSON)

    site = relationship("Site", back_populates="backup_snapshots")
