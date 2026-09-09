from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CustomerCallActivity(TimestampMixin, Base):
    """A call planned for a customer in the Hub."""

    __tablename__ = "customer_call_activities"
    __table_args__ = (Index("ix_customer_call_activities_customer_starts_at", "customer_id", "starts_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    direction: Mapped[str] = mapped_column(String(32), nullable=False, default="outbound")
    starts_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer(), nullable=False, default=30)
    reminder_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reminder_minutes_before: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    customer = relationship("Customer", back_populates="call_activities")
    reminders = relationship(
        "CustomerCallReminder",
        back_populates="call",
        cascade="all, delete-orphan",
        order_by="CustomerCallReminder.sort_order",
    )


class CustomerCallReminder(TimestampMixin, Base):
    """One scheduled reminder for a planned customer call."""

    __tablename__ = "customer_call_reminders"
    __table_args__ = (Index("ix_customer_call_reminders_call_sort_order", "call_id", "sort_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("customer_call_activities.id", ondelete="CASCADE"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    minutes_before: Mapped[int] = mapped_column(Integer(), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)

    call = relationship("CustomerCallActivity", back_populates="reminders")


class CustomerTaskActivity(TimestampMixin, Base):
    """A task planned for a customer in the Hub."""

    __tablename__ = "customer_task_activities"
    __table_args__ = (Index("ix_customer_task_activities_customer_due_at", "customer_id", "due_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("hub_cases.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    reminder_channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reminder_minutes_before: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    customer = relationship("Customer", back_populates="task_activities")


class CustomerMeetingActivity(TimestampMixin, Base):
    """A meeting planned for a customer in the Hub."""

    __tablename__ = "customer_meeting_activities"
    __table_args__ = (Index("ix_customer_meeting_activities_customer_starts_at", "customer_id", "starts_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    customer = relationship("Customer", back_populates="meeting_activities")
    reminders = relationship(
        "CustomerMeetingReminder",
        back_populates="meeting",
        cascade="all, delete-orphan",
        order_by="CustomerMeetingReminder.sort_order",
    )


class CustomerMeetingReminder(TimestampMixin, Base):
    """One scheduled reminder for a planned customer meeting."""

    __tablename__ = "customer_meeting_reminders"
    __table_args__ = (Index("ix_customer_meeting_reminders_meeting_sort_order", "meeting_id", "sort_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("customer_meeting_activities.id", ondelete="CASCADE"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    minutes_before: Mapped[int] = mapped_column(Integer(), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)

    meeting = relationship("CustomerMeetingActivity", back_populates="reminders")
