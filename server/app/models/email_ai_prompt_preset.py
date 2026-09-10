"""Configurable quick instructions for AI-assisted email editing."""

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class EmailAiPromptPreset(TimestampMixin, Base):
    """One user-visible shortcut that sends a fixed instruction to the email AI."""

    __tablename__ = "email_ai_prompt_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    instruction: Mapped[str] = mapped_column(Text(), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    is_enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
