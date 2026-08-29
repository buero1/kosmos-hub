from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class FleetRefreshRunStatus(StrEnum):
    queued = "queued"
    running = "running"
    cancelling = "cancelling"
    cancelled = "cancelled"
    succeeded = "succeeded"
    failed = "failed"


class FleetRefreshRun(TimestampMixin, Base):
    """Persisted full-fleet data refresh, separate from maintenance actions."""

    __tablename__ = "fleet_refresh_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    allow_provider_activation: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    site_results = relationship(
        "FleetRefreshSiteResult",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="FleetRefreshSiteResult.id.asc()",
    )


class FleetRefreshSiteResult(TimestampMixin, Base):
    """One website's outcome within a persisted fleet refresh run."""

    __tablename__ = "fleet_refresh_site_results"
    __table_args__ = (UniqueConstraint("fleet_refresh_run_id", "site_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fleet_refresh_run_id: Mapped[int] = mapped_column(ForeignKey("fleet_refresh_runs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    domain: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), index=True)
    state_status: Mapped[str] = mapped_column(String(32))
    updates_status: Mapped[str] = mapped_column(String(32))
    backups_status: Mapped[str] = mapped_column(String(32))
    users_status: Mapped[str] = mapped_column(String(32))
    jet_status: Mapped[str] = mapped_column(String(64), default="not-applicable")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text(), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)

    run = relationship("FleetRefreshRun", back_populates="site_results")
