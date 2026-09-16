"""Durable batches for combining Zoho Books and CRM orders in the Hub."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ZohoBooksOrderImport(TimestampMixin, Base):
    """A resumable import of every sales order in the selected Books organization."""

    __tablename__ = "zoho_books_order_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    total_orders: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    processed_orders: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    imported_orders: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    updated_orders: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    crm_matched_orders: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    crm_unmatched_orders: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    crm_ambiguous_orders: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_orders: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    items = relationship(
        "ZohoBooksOrderImportItem",
        back_populates="order_import",
        cascade="all, delete-orphan",
    )


class ZohoBooksOrderImportItem(TimestampMixin, Base):
    """One Books sales order and its optional unambiguous CRM counterpart."""

    __tablename__ = "zoho_books_order_import_items"
    __table_args__ = (
        UniqueConstraint(
            "order_import_id",
            "zoho_salesorder_id",
            name="uq_zb_order_import_item",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_import_id: Mapped[int] = mapped_column(
        ForeignKey("zoho_books_order_imports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    zoho_salesorder_id: Mapped[str] = mapped_column(String(255), nullable=False)
    zoho_books_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    zoho_crm_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    local_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_finance_orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    crm_match_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    order_import = relationship("ZohoBooksOrderImport", back_populates="items")
