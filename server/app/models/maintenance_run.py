from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class MaintenanceRunStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class MaintenanceRunStepStatus(StrEnum):
    waiting = "waiting"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class MaintenanceRun(TimestampMixin, Base):
    __tablename__ = "maintenance_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    bridge_backup_nonce: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)

    site = relationship("Site", back_populates="maintenance_runs")
    steps = relationship(
        "MaintenanceRunStep",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="MaintenanceRunStep.id.asc()",
    )


class MaintenanceRunStep(TimestampMixin, Base):
    __tablename__ = "maintenance_run_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    maintenance_run_id: Mapped[int] = mapped_column(ForeignKey("maintenance_runs.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text(), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)

    run = relationship("MaintenanceRun", back_populates="steps")
