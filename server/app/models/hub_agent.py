"""Persisted Hub-Agent conversations, turns and controlled actions."""

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubAgentConversation(TimestampMixin, Base):
    """One durable, user-owned Hub-Agent chat that can span multiple turns."""

    __tablename__ = "hub_agent_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    encrypted_title_json: Mapped[str] = mapped_column(Text(), nullable=False)

    jobs = relationship(
        "HubAgentJob",
        back_populates="conversation",
        order_by="HubAgentJob.created_at",
    )
    contexts = relationship(
        "HubAgentConversationContext",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="HubAgentConversationContext.created_at",
    )


class HubAgentConversationContext(TimestampMixin, Base):
    """A deliberately selected Hub entity and its encrypted context snapshot."""

    __tablename__ = "hub_agent_conversation_contexts"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "resource_type",
            "resource_key",
            name="uq_hub_agent_conversation_context_resource",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("hub_agent_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_type: Mapped[str] = mapped_column(String(48), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(512), nullable=False)
    encrypted_snapshot_json: Mapped[str] = mapped_column(Text(), nullable=False)

    conversation = relationship("HubAgentConversation", back_populates="contexts")


class HubAgentJob(TimestampMixin, Base):
    """One user instruction and the agent plan derived from it."""

    __tablename__ = "hub_agent_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_agent_conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready", index=True)
    encrypted_request_json: Mapped[str] = mapped_column(Text(), nullable=False)
    encrypted_plan_json: Mapped[str] = mapped_column(Text(), nullable=False)

    actions = relationship(
        "HubAgentAction",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="HubAgentAction.sort_order",
    )
    conversation = relationship("HubAgentConversation", back_populates="jobs")


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
