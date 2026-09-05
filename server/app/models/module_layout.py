from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ModuleLayout(TimestampMixin, Base):
    """A global, named order for reusable module items such as detail fields."""

    __tablename__ = "module_layouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    layout_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    item_order_json: Mapped[str] = mapped_column(Text, default="[]")
    configured_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("hub_users.id"), nullable=True)
