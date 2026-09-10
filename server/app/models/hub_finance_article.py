"""Hub-native article master data for the Finance area."""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubFinanceArticle(TimestampMixin, Base):
    """An article stored locally until the later Zoho Books import."""

    __tablename__ = "hub_finance_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    zoho_books_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
