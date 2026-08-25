from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SiteUpdateSnapshot(TimestampMixin, Base):
    __tablename__ = "site_update_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    core_updates_json: Mapped[list] = mapped_column(JSON)
    plugin_updates_json: Mapped[list] = mapped_column(JSON)
    theme_updates_json: Mapped[list] = mapped_column(JSON)
    summary_json: Mapped[dict] = mapped_column(JSON)

    site = relationship("Site", back_populates="update_snapshots")
