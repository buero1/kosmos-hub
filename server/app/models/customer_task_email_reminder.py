from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CustomerTaskEmailReminder(TimestampMixin, Base):
    """A durable, one-time internal email reminder for a customer task."""

    __tablename__ = "customer_task_email_reminders"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_customer_task_email_reminders_task"),
        Index("ix_customer_task_email_reminders_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_task_activities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    mailbox_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_mailbox_emails.id", ondelete="SET NULL"),
        nullable=True,
    )
    creator_username: Mapped[str] = mapped_column(String(64), nullable=False)
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    task_description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    minutes_before: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    attempt_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
