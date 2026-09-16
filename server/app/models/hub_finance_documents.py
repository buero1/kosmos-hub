"""Hub-native Finance documents that share the article position structure."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubFinanceOrder(TimestampMixin, Base):
    __tablename__ = "hub_finance_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_offers.id", ondelete="SET NULL"), nullable=True, index=True)
    pdf_template_id: Mapped[int | None] = mapped_column(ForeignKey("hub_pdf_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    zoho_books_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    zoho_crm_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer")
    contact = relationship("CustomerContact")
    offer = relationship("HubFinanceOffer")
    pdf_template = relationship("HubPdfTemplate")
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
    __table_args__ = (
        UniqueConstraint("recurring_invoice_id", "recurring_scheduled_on", name="uq_hub_finance_invoices_recurring_occurrence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_orders.id", ondelete="SET NULL"), nullable=True, index=True)
    recurring_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_recurring_invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    recurring_scheduled_on: Mapped[date | None] = mapped_column(Date(), nullable=True)
    pdf_template_id: Mapped[int | None] = mapped_column(ForeignKey("hub_pdf_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    zoho_books_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer")
    contact = relationship("CustomerContact")
    order = relationship("HubFinanceOrder")
    recurring_invoice = relationship("HubFinanceRecurringInvoice", back_populates="generated_invoices")
    pdf_template = relationship("HubPdfTemplate")
    dunnings = relationship("HubFinanceDunning", back_populates="invoice", order_by="HubFinanceDunning.created_at")
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


class HubFinanceDunning(TimestampMixin, Base):
    __tablename__ = "hub_finance_dunnings"

    id: Mapped[int] = mapped_column(primary_key=True)
    dunning_number: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    pdf_template_id: Mapped[int | None] = mapped_column(ForeignKey("hub_pdf_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)

    customer = relationship("Customer")
    contact = relationship("CustomerContact")
    invoice = relationship("HubFinanceInvoice", back_populates="dunnings")
    pdf_template = relationship("HubPdfTemplate")
    lines = relationship("HubFinanceDunningLine", back_populates="dunning", cascade="all, delete-orphan", order_by="HubFinanceDunningLine.position_index")


class HubFinanceDunningLine(TimestampMixin, Base):
    __tablename__ = "hub_finance_dunning_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    dunning_id: Mapped[int] = mapped_column(ForeignKey("hub_finance_dunnings.id", ondelete="CASCADE"), nullable=False, index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("hub_finance_articles.id", ondelete="SET NULL"), nullable=True, index=True)
    position_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)

    dunning = relationship("HubFinanceDunning", back_populates="lines")
    article = relationship("HubFinanceArticle")


class HubFinanceRecurringInvoice(TimestampMixin, Base):
    __tablename__ = "hub_finance_recurring_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    pdf_template_id: Mapped[int | None] = mapped_column(ForeignKey("hub_pdf_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    zoho_books_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    hub_next_run_on: Mapped[date | None] = mapped_column(Date(), nullable=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zoho_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer")
    contact = relationship("CustomerContact")
    pdf_template = relationship("HubPdfTemplate")
    generated_invoices = relationship("HubFinanceInvoice", back_populates="recurring_invoice", order_by="HubFinanceInvoice.recurring_scheduled_on")
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
