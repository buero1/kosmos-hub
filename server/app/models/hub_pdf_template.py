"""Versioned, Hub-native templates for Finance PDFs."""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class HubPdfTemplate(TimestampMixin, Base):
    """The current editable state of one Finance PDF template."""

    __tablename__ = "hub_pdf_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False, index=True)
    version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    content_json: Mapped[str] = mapped_column(Text(), nullable=False)
    legal_terms_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_legal_terms.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_username: Mapped[str] = mapped_column(String(255), nullable=False)

    legal_terms = relationship("HubLegalTerms")
    revisions = relationship(
        "HubPdfTemplateRevision",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="HubPdfTemplateRevision.version",
    )


class HubPdfTemplateRevision(TimestampMixin, Base):
    """Immutable snapshot used to reproduce documents created with an older template."""

    __tablename__ = "hub_pdf_template_revisions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_hub_pdf_template_revisions_template_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("hub_pdf_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_json: Mapped[str] = mapped_column(Text(), nullable=False)
    legal_terms_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("hub_legal_terms_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_username: Mapped[str] = mapped_column(String(255), nullable=False)

    legal_terms_revision = relationship("HubLegalTermsRevision")
    template = relationship("HubPdfTemplate", back_populates="revisions")
