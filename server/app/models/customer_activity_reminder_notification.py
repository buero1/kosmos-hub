from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CustomerActivityReminderNotification(TimestampMixin, Base):
    """Tracks one desktop-popup reminder and its snooze or completion state."""

    __tablename__ = "customer_activity_reminder_notifications"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "activity_kind",
            "activity_id",
            "reminder_key",
            name="uq_customer_activity_reminder_notifications_source",
        ),
        Index("ix_customer_activity_reminder_notifications_due", "user_id", "completed_at", "snoozed_until", "remind_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("hub_users.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    activity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    activity_id: Mapped[int] = mapped_column(Integer(), nullable=False)
    reminder_key: Mapped[str] = mapped_column(String(32), nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
