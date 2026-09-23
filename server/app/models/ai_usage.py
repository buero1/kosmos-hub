"""Content-free accounting, independent of chat retention and business writes."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, JSON, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AiUsageRequest(Base):
    __tablename__ = "ai_usage_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    feature: Mapped[str] = mapped_column(String(48))
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), default="started")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 8), nullable=True)
    estimated_usd_upper: Mapped[Decimal | None] = mapped_column(Numeric(16, 8), nullable=True)
    pricing_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sizes: Mapped[dict] = mapped_column(JSON, default=dict)
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
