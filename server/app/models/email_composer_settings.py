from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class EmailComposerSettings(TimestampMixin, Base):
    """The single Hub-wide default style for newly composed emails."""

    __tablename__ = "email_composer_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    font_family_key: Mapped[str] = mapped_column(String(32), nullable=False, default="verdana")
    font_size: Mapped[int] = mapped_column(Integer(), nullable=False, default=12)
    line_height: Mapped[float] = mapped_column(Float(), nullable=False, default=1.1)
    signature_html: Mapped[str] = mapped_column(Text(), nullable=False, default="")
