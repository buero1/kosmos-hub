from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubWorkflow(TimestampMixin, Base):
    """One fixed Hub automation that can be documented in Account."""

    __tablename__ = "hub_workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    module_label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text(), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
