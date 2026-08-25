from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class UpdatePlanStatus(StrEnum):
    draft = "draft"
    approved = "approved"
    blocked = "blocked"
    executed = "executed"
    failed = "failed"
    postflight_failed = "postflight_failed"


class UpdatePlan(TimestampMixin, Base):
    __tablename__ = "update_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=UpdatePlanStatus.draft.value, index=True)
    created_by: Mapped[str] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)

    items = relationship("UpdatePlanItem", back_populates="plan", cascade="all, delete-orphan")


class UpdatePlanItem(TimestampMixin, Base):
    __tablename__ = "update_plan_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("update_plans.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    update_type: Mapped[str] = mapped_column(String(32))
    update_identifier: Mapped[str] = mapped_column(String(255))
    update_name: Mapped[str] = mapped_column(String(255))
    current_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    snapshot_captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    plan = relationship("UpdatePlan", back_populates="items")
    site = relationship("Site", back_populates="update_plan_items")
