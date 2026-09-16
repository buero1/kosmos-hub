"""Reusable encrypted Finance position packages."""

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubFinancePositionPreset(TimestampMixin, Base):
    """A named snapshot of editable Finance document line items."""

    __tablename__ = "hub_finance_position_presets"
    __table_args__ = (
        UniqueConstraint(
            "library_key",
            "normalized_name",
            name="uq_hub_finance_position_presets_library_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    library_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_lines_json: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by_username: Mapped[str] = mapped_column(String(255), nullable=False)
