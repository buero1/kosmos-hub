from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubTeam(TimestampMixin, Base):
    __tablename__ = "hub_teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(96), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)


class HubAccessRole(TimestampMixin, Base):
    __tablename__ = "hub_access_roles"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(96), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    is_system: Mapped[bool] = mapped_column(Boolean(), default=False)
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)


class HubRolePermission(TimestampMixin, Base):
    __tablename__ = "hub_role_permissions"
    __table_args__ = (
        UniqueConstraint("role_key", "module_key", name="uq_hub_role_permissions_role_module"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    role_key: Mapped[str] = mapped_column(ForeignKey("hub_access_roles.key", ondelete="CASCADE"), index=True)
    module_key: Mapped[str] = mapped_column(String(48), index=True)
    can_view: Mapped[bool] = mapped_column(Boolean(), default=False)
    can_create: Mapped[bool] = mapped_column(Boolean(), default=False)
    can_edit: Mapped[bool] = mapped_column(Boolean(), default=False)
    can_delete: Mapped[bool] = mapped_column(Boolean(), default=False)
    can_export: Mapped[bool] = mapped_column(Boolean(), default=False)
    can_manage: Mapped[bool] = mapped_column(Boolean(), default=False)
    can_send: Mapped[bool] = mapped_column(Boolean(), default=False)
    record_scope: Mapped[str] = mapped_column(String(16), default="none")


class HubRecordAssignment(TimestampMixin, Base):
    __tablename__ = "hub_record_assignments"
    __table_args__ = (
        UniqueConstraint("module_key", "record_id", name="uq_hub_record_assignments_module_record"),
        Index("ix_hub_record_assignments_owner", "module_key", "owner_user_id"),
        Index("ix_hub_record_assignments_team", "module_key", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    module_key: Mapped[str] = mapped_column(String(48))
    record_id: Mapped[int] = mapped_column(index=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id", ondelete="SET NULL"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("hub_teams.id", ondelete="SET NULL"), nullable=True)


class HubRecordAccessGrant(TimestampMixin, Base):
    __tablename__ = "hub_record_access_grants"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND team_id IS NULL) OR (user_id IS NULL AND team_id IS NOT NULL)",
            name="target_exactly_one",
        ),
        Index("ix_hub_record_access_grants_record", "module_key", "record_id"),
        Index("ix_hub_record_access_grants_user", "module_key", "user_id"),
        Index("ix_hub_record_access_grants_team", "module_key", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    module_key: Mapped[str] = mapped_column(String(48))
    record_id: Mapped[int] = mapped_column(index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id", ondelete="CASCADE"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("hub_teams.id", ondelete="CASCADE"), nullable=True)
    can_edit: Mapped[bool] = mapped_column(Boolean(), default=False)
