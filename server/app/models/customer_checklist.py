from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CustomerChecklist(TimestampMixin, Base):
    __tablename__ = "customer_checklists"
    __table_args__ = (Index("ix_customer_checklists_customer_sort", "customer_id", "sort_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)

    customer = relationship("Customer", back_populates="checklists")
    items = relationship(
        "CustomerChecklistItem",
        back_populates="checklist",
        cascade="all, delete-orphan",
        order_by="CustomerChecklistItem.sort_order, CustomerChecklistItem.id",
    )


class CustomerChecklistItem(TimestampMixin, Base):
    __tablename__ = "customer_checklist_items"
    __table_args__ = (Index("ix_customer_checklist_items_checklist_sort", "checklist_id", "sort_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    checklist_id: Mapped[int] = mapped_column(
        ForeignKey("customer_checklists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    checklist = relationship("CustomerChecklist", back_populates="items")
