from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubSpamSender(TimestampMixin, Base):
    """An exact sender address blocked independently of stored messages."""

    __tablename__ = "hub_spam_senders"
    __table_args__ = (UniqueConstraint("email_address", name="uq_hub_spam_senders_email_address"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
