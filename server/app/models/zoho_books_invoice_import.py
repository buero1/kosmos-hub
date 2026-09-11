"""Durable, resumable batches for importing Zoho Books invoice snapshots."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ZohoBooksInvoiceImport(TimestampMixin, Base):
    """A requested batch of the most recent Zoho Books invoices."""

    __tablename__ = "zoho_books_invoice_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    requested_limit: Mapped[int] = mapped_column(Integer(), nullable=False)
    total_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    processed_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    imported_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    updated_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    stored_pdfs: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    unavailable_pdfs: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_invoices: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    items = relationship("ZohoBooksInvoiceImportItem", back_populates="invoice_import", cascade="all, delete-orphan")


class ZohoBooksInvoiceImportItem(TimestampMixin, Base):
    """One source invoice selected for an import batch."""

    __tablename__ = "zoho_books_invoice_import_items"
    __table_args__ = (
        UniqueConstraint(
            "invoice_import_id",
            "zoho_invoice_id",
            name="uq_zoho_books_invoice_import_items_import_invoice",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_import_id: Mapped[int] = mapped_column(
        ForeignKey("zoho_books_invoice_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    zoho_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    local_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_finance_invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    pdf_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    invoice_import = relationship("ZohoBooksInvoiceImport", back_populates="items")
