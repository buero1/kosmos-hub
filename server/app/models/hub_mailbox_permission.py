"""Mailbox grants and trusted message membership, separate from CRM relations."""
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubMailboxPermission(TimestampMixin, Base):
    __tablename__ = "hub_mailbox_permissions"
    __table_args__ = (
        CheckConstraint("(user_id IS NULL) <> (team_id IS NULL)", name="mailbox_permission_subject").ddl_if(dialect=("sqlite", "postgresql")),
        UniqueConstraint("mailbox_account_id", "user_id", name="uq_mailbox_permission_user"),
        UniqueConstraint("mailbox_account_id", "team_id", name="uq_mailbox_permission_team"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_account_id: Mapped[int] = mapped_column(ForeignKey("hub_mailbox_accounts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("hub_teams.id", ondelete="CASCADE"), index=True)
    # NULL inherits; False is an explicit user denial, including inherited grants.
    can_view: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    can_create: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    can_edit: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    can_send: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    can_delete: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)


class HubMailboxMembership(TimestampMixin, Base):
    __tablename__ = "hub_mailbox_memberships"
    __table_args__ = (
        CheckConstraint("(customer_email_id IS NOT NULL) + (mailbox_email_id IS NOT NULL) + (lead_email_id IS NOT NULL) + (scheduled_email_id IS NOT NULL) = 1", name="mailbox_membership_message").ddl_if(dialect="sqlite"),
        *(UniqueConstraint("mailbox_account_id", column, name=f"uq_mailbox_membership_{name}") for name, column in (
            ("customer", "customer_email_id"), ("mailbox", "mailbox_email_id"), ("lead", "lead_email_id"), ("scheduled", "scheduled_email_id"))),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_account_id: Mapped[int] = mapped_column(ForeignKey("hub_mailbox_accounts.id", ondelete="CASCADE"), index=True)
    customer_email_id: Mapped[int | None] = mapped_column(ForeignKey("customer_zoho_emails.id", ondelete="CASCADE"), index=True)
    mailbox_email_id: Mapped[int | None] = mapped_column(ForeignKey("hub_mailbox_emails.id", ondelete="CASCADE"), index=True)
    lead_email_id: Mapped[int | None] = mapped_column(ForeignKey("hub_lead_emails.id", ondelete="CASCADE"), index=True)
    scheduled_email_id: Mapped[int | None] = mapped_column(ForeignKey("hub_scheduled_emails.id", ondelete="CASCADE"), index=True)


# MySQL prohibits CHECK constraints on columns with cascading foreign keys.
# Keep cascades and enforce the same single-target invariant on ORM writes.
@event.listens_for(HubMailboxPermission, "before_insert")
@event.listens_for(HubMailboxPermission, "before_update")
@event.listens_for(HubMailboxMembership, "before_insert")
@event.listens_for(HubMailboxMembership, "before_update")
def validate_single_target(mapper, connection, record):
    fields = ("user_id", "team_id") if isinstance(record, HubMailboxPermission) else (
        "customer_email_id", "mailbox_email_id", "lead_email_id", "scheduled_email_id")
    if sum(getattr(record, field) is not None for field in fields) != 1:
        raise ValueError("Exactly one mailbox permission subject or message is required.")
