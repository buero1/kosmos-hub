"""Single-use receipt ledger; no plaintext tokens or bank details."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

PRECISE_TIME = DateTime(timezone=True).with_variant(DATETIME(fsp=6), "mysql")


class HubSepaSubmission(Base):
    __tablename__ = "hub_sepa_submissions"

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(PRECISE_TIME)
    received_at: Mapped[datetime | None] = mapped_column(PRECISE_TIME, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(PRECISE_TIME, nullable=True)
    payload_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
