"""Encrypted locally stored PDF snapshots for imported Finance orders."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubFinanceOrderPdf(TimestampMixin, Base):
    """One locally stored order PDF; the encrypted bytes stay outside MySQL."""

    __tablename__ = "hub_finance_order_pdfs"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("hub_finance_orders.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="zoho-books")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/pdf")
    byte_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
