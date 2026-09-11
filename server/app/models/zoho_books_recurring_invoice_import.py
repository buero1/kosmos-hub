"""Durable batches for importing Zoho Books recurring invoice snapshots."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ZohoBooksRecurringInvoiceImport(TimestampMixin, Base):
    """A resumable import of every recurring invoice in the selected Books organization."""

    __tablename__ = "zoho_books_recurring_invoice_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    total_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    processed_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    imported_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    updated_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    items = relationship(
        "ZohoBooksRecurringInvoiceImportItem",
        back_populates="recurring_invoice_import",
        cascade="all, delete-orphan",
    )


class ZohoBooksRecurringInvoiceImportItem(TimestampMixin, Base):
    """One source profile selected for a recurring invoice import batch."""

    __tablename__ = "zoho_books_recurring_invoice_import_items"
    __table_args__ = (
        UniqueConstraint(
            "recurring_invoice_import_id",
            "zoho_recurring_invoice_id",
            name="uq_zb_recurring_import_item",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    recurring_invoice_import_id: Mapped[int] = mapped_column(
        ForeignKey("zoho_books_recurring_invoice_imports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    zoho_recurring_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    local_recurring_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_finance_recurring_invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    recurring_invoice_import = relationship(
        "ZohoBooksRecurringInvoiceImport",
        back_populates="items",
    )
