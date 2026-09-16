from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubIntegrationToken(TimestampMixin, Base):
    """Revocable machine credential for one external system integration."""

    __tablename__ = "hub_integration_tokens"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_hub_integration_tokens_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("hub_users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    source_key: Mapped[str] = mapped_column(String(48), nullable=False, default="callapp")
    token_prefix: Mapped[str] = mapped_column(String(24))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
