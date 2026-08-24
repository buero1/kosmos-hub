from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SiteCapability(TimestampMixin, Base):
    __tablename__ = "site_capabilities"
    __table_args__ = (
        UniqueConstraint("site_id", "provider", "ability_name", name="uq_site_capabilities_site_provider_ability"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    capability: Mapped[str] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    ability_name: Mapped[str] = mapped_column(String(255))
    ability_schema: Mapped[dict] = mapped_column(JSON)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    destructive: Mapped[bool] = mapped_column(Boolean, default=False)
    last_discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    site = relationship("Site", back_populates="capabilities")
