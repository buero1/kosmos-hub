from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class HubEmailTemplateFolder(TimestampMixin, Base):
    """A persistent folder that can exist independently of email templates."""

    __tablename__ = "hub_email_template_folders"
    __table_args__ = (UniqueConstraint("name", name="uq_hub_email_template_folders_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
