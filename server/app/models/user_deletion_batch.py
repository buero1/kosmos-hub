from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class UserDeletionBatch(TimestampMixin, Base):
    """A recoverable, explicitly reviewed WordPress user deletion batch."""

    __tablename__ = "user_deletion_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items = relationship(
        "UserDeletionBatchItem",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="UserDeletionBatchItem.position.asc()",
    )


class UserDeletionBatchItem(TimestampMixin, Base):
    """One user deletion and its individual content reassignment target."""

    __tablename__ = "user_deletion_batch_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_deletion_batch_id: Mapped[int] = mapped_column(
        Integer(),
        ForeignKey("user_deletion_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    site_id: Mapped[int] = mapped_column(Integer(), ForeignKey("sites.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer(), nullable=False)
    target_user_id: Mapped[int] = mapped_column(Integer(), nullable=False)
    target_username: Mapped[str] = mapped_column(String(255), nullable=False)
    replacement_user_id: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    replacement_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch = relationship("UserDeletionBatch", back_populates="items")
    site = relationship("Site")
