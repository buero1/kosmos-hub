from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ZohoEmailAttachmentImport(TimestampMixin, Base):
    """A resumable batch for copying Zoho email attachments into Hub storage."""

    __tablename__ = "zoho_email_attachment_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    continue_automatically: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    requested_limit: Mapped[int] = mapped_column(Integer(), nullable=False)
    total_attachments: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    processed_attachments: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    stored_attachments: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_attachments: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    stored_bytes: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    items = relationship("ZohoEmailAttachmentImportItem", back_populates="attachment_import", cascade="all, delete-orphan")


class ZohoEmailAttachmentImportItem(TimestampMixin, Base):
    """One attachment selected for a durable import batch."""

    __tablename__ = "zoho_email_attachment_import_items"
    __table_args__ = (
        UniqueConstraint(
            "attachment_import_id",
            "email_id",
            "source_attachment_id",
            name="uq_zoho_email_attachment_import_items_import_attachment",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_import_id: Mapped[int] = mapped_column(
        ForeignKey("zoho_email_attachment_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email_id: Mapped[int] = mapped_column(
        ForeignKey("customer_zoho_emails.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_attachment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    attachment_import = relationship("ZohoEmailAttachmentImport", back_populates="items")
