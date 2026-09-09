from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubCaseEmailLink(TimestampMixin, Base):
    """One explicitly chosen email that belongs to a Hub case."""

    __tablename__ = "hub_case_email_links"
    __table_args__ = (
        UniqueConstraint("case_id", "customer_email_id", name="uq_hub_case_email_links_case_customer_email"),
        UniqueConstraint("case_id", "mailbox_email_id", name="uq_hub_case_email_links_case_mailbox_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("hub_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_zoho_emails.id", ondelete="CASCADE"), nullable=True, index=True
    )
    mailbox_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_mailbox_emails.id", ondelete="CASCADE"), nullable=True, index=True
    )

    case = relationship("HubCase", back_populates="email_links")
    customer_email = relationship("CustomerZohoEmail")
    mailbox_email = relationship("HubMailboxEmail")
