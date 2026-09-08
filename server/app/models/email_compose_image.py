from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class EmailComposeImage(TimestampMixin, Base):
    """An encrypted image uploaded for use inside a Hub-composed email."""

    __tablename__ = "email_compose_images"
    __table_args__ = (
        UniqueConstraint("token", name="uq_email_compose_images_token"),
        UniqueConstraint("storage_key", name="uq_email_compose_images_storage_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(96), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("hub_users.id"), nullable=False)
