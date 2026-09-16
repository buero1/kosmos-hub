"""Generated Finance PDF snapshots and their generation state."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubFinanceGeneratedPdf(TimestampMixin, Base):
    """The latest generated PDF for one Hub Finance document."""

    __tablename__ = "hub_finance_generated_pdfs"
    __table_args__ = (
        UniqueConstraint("document_type", "document_id", name="uq_hub_finance_generated_pdfs_document"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    document_id: Mapped[int] = mapped_column(Integer(), nullable=False, index=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_pdf_templates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    template_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_pdf_template_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    template_name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    generation_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/pdf")
    byte_size: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    is_zugferd: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    zugferd_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    zugferd_profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not-applicable")
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
