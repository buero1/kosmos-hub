"""Address lookup index; messages and attachments retain their original identity."""
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class HubEmailAddress(Base):
    __tablename__ = "hub_email_addresses"
    __table_args__ = (
        CheckConstraint("(customer_email_id IS NULL) <> (mailbox_email_id IS NULL)", name="one_source"),
        UniqueConstraint("customer_email_id", "address_digest"),
        UniqueConstraint("mailbox_email_id", "address_digest"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_email_id: Mapped[int | None] = mapped_column(ForeignKey("customer_zoho_emails.id", ondelete="CASCADE"), index=True)
    mailbox_email_id: Mapped[int | None] = mapped_column(ForeignKey("hub_mailbox_emails.id", ondelete="CASCADE"), index=True)
    address_digest: Mapped[str] = mapped_column(String(64), index=True)
