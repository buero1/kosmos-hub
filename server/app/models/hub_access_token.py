from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubAccessToken(TimestampMixin, Base):
    __tablename__ = "hub_access_tokens"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_hub_access_tokens_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("hub_users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    token_prefix: Mapped[str] = mapped_column(String(24))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
