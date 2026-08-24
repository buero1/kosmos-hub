from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    result: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text(), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    site = relationship("Site", back_populates="audit_entries")

