"""Persisted plans and controlled actions proposed by the Hub agent."""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubAgentJob(TimestampMixin, Base):
    """One user instruction and the agent plan derived from it."""

    __tablename__ = "hub_agent_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready", index=True)
    encrypted_request_json: Mapped[str] = mapped_column(Text(), nullable=False)
    encrypted_plan_json: Mapped[str] = mapped_column(Text(), nullable=False)

    actions = relationship(
        "HubAgentAction",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="HubAgentAction.sort_order",
    )


class HubAgentAction(TimestampMixin, Base):
    """A single validated operation which needs an explicit execute click."""

    __tablename__ = "hub_agent_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("hub_agent_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed", index=True)
    encrypted_payload_json: Mapped[str] = mapped_column(Text(), nullable=False)
    encrypted_result_json: Mapped[str | None] = mapped_column(Text(), nullable=True)

    job = relationship("HubAgentJob", back_populates="actions")
