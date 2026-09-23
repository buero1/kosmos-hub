"""Transactionally maintained provenance, independent of a module's storage format."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class HubRecordInfo(Base):
    __tablename__ = "hub_record_info"

    record_table: Mapped[str] = mapped_column(String(96), primary_key=True)
    record_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id", ondelete="SET NULL"), nullable=True)
    created_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_origin: Mapped[str | None] = mapped_column(String(24), nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id", ondelete="SET NULL"), nullable=True)
    changed_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_origin: Mapped[str | None] = mapped_column(String(24), nullable=True)
