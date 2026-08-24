from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SiteSnapshot(TimestampMixin, Base):
    __tablename__ = "site_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    wordpress_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    php_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plugins_json: Mapped[list] = mapped_column(JSON)
    themes_json: Mapped[list] = mapped_column(JSON)
    environment_json: Mapped[dict] = mapped_column(JSON)

    site = relationship("Site", back_populates="snapshots")
