from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ZohoEmailContentImport(TimestampMixin, Base):
    """A durable batch for loading full message bodies from existing Zoho headers."""

    __tablename__ = "zoho_email_content_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requested_limit: Mapped[int] = mapped_column(Integer(), nullable=False)
    total_emails: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    processed_emails: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    loaded_emails: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_emails: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    items = relationship("ZohoEmailContentImportItem", back_populates="email_import", cascade="all, delete-orphan")


class ZohoEmailContentImportItem(TimestampMixin, Base):
    """One selected email body in a content-import batch."""

    __tablename__ = "zoho_email_content_import_items"
    __table_args__ = (
        UniqueConstraint("email_import_id", "email_id", name="uq_zoho_email_content_import_items_import_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_import_id: Mapped[int] = mapped_column(
        ForeignKey("zoho_email_content_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email_id: Mapped[int] = mapped_column(
        ForeignKey("customer_zoho_emails.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    email_import = relationship("ZohoEmailContentImport", back_populates="items")
