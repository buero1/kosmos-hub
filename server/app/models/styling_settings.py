from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class StylingSettings(TimestampMixin, Base):
    """Singleton configuration for the Hub's shared visual tokens."""

    __tablename__ = "styling_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    font_family_key: Mapped[str] = mapped_column(String(32), default="serif")
    background_color: Mapped[str] = mapped_column(String(7), default="#f5f1e8")
    background_secondary_color: Mapped[str] = mapped_column(String(7), default="#efe8da")
    panel_color: Mapped[str] = mapped_column(String(7), default="#fffaf2")
    ink_color: Mapped[str] = mapped_column(String(7), default="#1d2a2f")
    muted_color: Mapped[str] = mapped_column(String(7), default="#6c7469")
    accent_color: Mapped[str] = mapped_column(String(7), default="#0e7c66")
    accent_soft_color: Mapped[str] = mapped_column(String(7), default="#d7efe8")
    border_color: Mapped[str] = mapped_column(String(7), default="#d9d2c5")
    base_spacing: Mapped[int] = mapped_column(Integer, default=16)
    panel_radius: Mapped[int] = mapped_column(Integer, default=18)
    control_v1_height: Mapped[int] = mapped_column(Integer, default=38)
    control_v1_font_size: Mapped[int] = mapped_column(Integer, default=13)
    control_v1_padding: Mapped[int] = mapped_column(Integer, default=12)
    control_v1_radius: Mapped[int] = mapped_column(Integer, default=5)
    control_v2_height: Mapped[int] = mapped_column(Integer, default=30)
    control_v2_font_size: Mapped[int] = mapped_column(Integer, default=12)
    control_v2_padding: Mapped[int] = mapped_column(Integer, default=10)
    control_v2_radius: Mapped[int] = mapped_column(Integer, default=3)
    configured_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id"), nullable=True)
