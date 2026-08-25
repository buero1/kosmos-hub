from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SiteStatus(StrEnum):
    pending = "pending"
    verified = "verified"
    unknown = "unknown"
    unavailable = "unavailable"


class Site(TimestampMixin, Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    home_url: Mapped[str] = mapped_column(Text())
    site_url: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(32), default=SiteStatus.pending.value)
    wordpress_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    php_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bridge_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer", back_populates="sites")
    connections = relationship("SiteConnection", back_populates="site", cascade="all, delete-orphan")
    audit_entries = relationship("AuditLog", back_populates="site", cascade="all, delete-orphan")
    capabilities = relationship("SiteCapability", back_populates="site", cascade="all, delete-orphan")
    snapshots = relationship("SiteSnapshot", back_populates="site", cascade="all, delete-orphan", order_by="desc(SiteSnapshot.captured_at)")
    update_snapshots = relationship(
        "SiteUpdateSnapshot",
        back_populates="site",
        cascade="all, delete-orphan",
        order_by="desc(SiteUpdateSnapshot.captured_at)",
    )
    backup_snapshots = relationship(
        "SiteBackupSnapshot",
        back_populates="site",
        cascade="all, delete-orphan",
        order_by="desc(SiteBackupSnapshot.captured_at)",
    )
    maintenance_runs = relationship("MaintenanceRun", back_populates="site", cascade="all, delete-orphan")
    update_plan_items = relationship("UpdatePlanItem", back_populates="site", cascade="all, delete-orphan")
