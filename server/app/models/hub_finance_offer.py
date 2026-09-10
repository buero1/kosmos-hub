"""Hub-native offer records and their line items."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubFinanceOffer(TimestampMixin, Base):
    """A Finance offer with immutable, local line-item data."""

    __tablename__ = "hub_finance_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_number: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    zoho_books_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer")
    contact = relationship("CustomerContact")
    lines = relationship(
        "HubFinanceOfferLine",
        back_populates="offer",
        cascade="all, delete-orphan",
        order_by="HubFinanceOfferLine.position_index",
    )


class HubFinanceOfferLine(TimestampMixin, Base):
    """One repeated article row of a Finance offer."""

    __tablename__ = "hub_finance_offer_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("hub_finance_offers.id", ondelete="CASCADE"), nullable=False, index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_articles.id", ondelete="SET NULL"), nullable=True, index=True)
    position_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)

    offer = relationship("HubFinanceOffer", back_populates="lines")
    article = relationship("HubFinanceArticle")
