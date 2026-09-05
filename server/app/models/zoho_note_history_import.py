from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ZohoNoteHistoryImport(TimestampMixin, Base):
    """Durable progress for the one-time import of every Zoho Account note."""

    __tablename__ = "zoho_note_history_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    total_customers: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    processed_customers: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    imported_notes: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    last_customer_id: Mapped[int | None] = mapped_column(Integer(), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
