from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubCase(TimestampMixin, Base):
    """A Hub-native case with a customer link and encrypted case fields."""

    __tablename__ = "hub_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_number: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    encrypted_fields_json: Mapped[str] = mapped_column(Text(), nullable=False)

    customer = relationship("Customer")
