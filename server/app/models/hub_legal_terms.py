"""Versioned terms and conditions maintained in the Hub."""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubLegalTerms(TimestampMixin, Base):
    """The current editable state of one terms-and-conditions document."""

    __tablename__ = "hub_legal_terms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    content_html: Mapped[str] = mapped_column(Text(), nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False, index=True)
    created_by_username: Mapped[str] = mapped_column(String(255), nullable=False)

    revisions = relationship(
        "HubLegalTermsRevision",
        back_populates="legal_terms",
        cascade="all, delete-orphan",
        order_by="HubLegalTermsRevision.version",
    )


class HubLegalTermsRevision(TimestampMixin, Base):
    """Immutable AGB snapshot referenced by a PDF-template revision."""

    __tablename__ = "hub_legal_terms_revisions"
    __table_args__ = (
        UniqueConstraint("legal_terms_id", "version", name="uq_hub_legal_terms_revisions_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    legal_terms_id: Mapped[int] = mapped_column(
        ForeignKey("hub_legal_terms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_html: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by_username: Mapped[str] = mapped_column(String(255), nullable=False)

    legal_terms = relationship("HubLegalTerms", back_populates="revisions")
