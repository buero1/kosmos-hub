"""Hub-native Finance documents that share the article position structure."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubFinanceOrder(TimestampMixin, Base):
    __tablename__ = "hub_finance_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_offers.id", ondelete="SET NULL"), nullable=True, index=True)
    zoho_books_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer")
    contact = relationship("CustomerContact")
    offer = relationship("HubFinanceOffer")
    lines = relationship("HubFinanceOrderLine", back_populates="order", cascade="all, delete-orphan", order_by="HubFinanceOrderLine.position_index")


class HubFinanceOrderLine(TimestampMixin, Base):
    __tablename__ = "hub_finance_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("hub_finance_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_articles.id", ondelete="SET NULL"), nullable=True, index=True)
    position_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)

    order = relationship("HubFinanceOrder", back_populates="lines")
    article = relationship("HubFinanceArticle")


class HubFinanceInvoice(TimestampMixin, Base):
    __tablename__ = "hub_finance_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_orders.id", ondelete="SET NULL"), nullable=True, index=True)
    zoho_books_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer")
    contact = relationship("CustomerContact")
    order = relationship("HubFinanceOrder")
    lines = relationship("HubFinanceInvoiceLine", back_populates="invoice", cascade="all, delete-orphan", order_by="HubFinanceInvoiceLine.position_index")


class HubFinanceInvoiceLine(TimestampMixin, Base):
    __tablename__ = "hub_finance_invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("hub_finance_invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_articles.id", ondelete="SET NULL"), nullable=True, index=True)
    position_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)

    invoice = relationship("HubFinanceInvoice", back_populates="lines")
    article = relationship("HubFinanceArticle")


class HubFinanceRecurringInvoice(TimestampMixin, Base):
    __tablename__ = "hub_finance_recurring_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    zoho_books_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer")
    contact = relationship("CustomerContact")
    lines = relationship("HubFinanceRecurringInvoiceLine", back_populates="recurring_invoice", cascade="all, delete-orphan", order_by="HubFinanceRecurringInvoiceLine.position_index")


class HubFinanceRecurringInvoiceLine(TimestampMixin, Base):
    __tablename__ = "hub_finance_recurring_invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    recurring_invoice_id: Mapped[int] = mapped_column(ForeignKey("hub_finance_recurring_invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_articles.id", ondelete="SET NULL"), nullable=True, index=True)
    position_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)

    recurring_invoice = relationship("HubFinanceRecurringInvoice", back_populates="lines")
    article = relationship("HubFinanceArticle")
