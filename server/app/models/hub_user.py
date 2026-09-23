from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubUser(TimestampMixin, Base):
    __tablename__ = "hub_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(64), default="admin")
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)
    reminder_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    default_sender_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_mailbox_accounts.id", ondelete="SET NULL", use_alter=True, name="fk_hub_user_default_sender"), nullable=True
    )
    mailbox_alert_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    session_version: Mapped[int] = mapped_column(Integer(), default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def display_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part) or self.username
