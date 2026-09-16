from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class HubActivityEvent(Base):
    __tablename__ = "hub_activity_events"
    __table_args__ = (
        Index("ix_hub_activity_module_time", "module_key", "id"),
        Index("ix_hub_activity_record_time", "module_key", "resource_id", "id"),
        Index("ix_hub_activity_actor_time", "actor", "id"),
        Index("ix_hub_activity_timestamp", "timestamp", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    module_key: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    changed_fields: Mapped[str | None] = mapped_column(Text(), nullable=True)
