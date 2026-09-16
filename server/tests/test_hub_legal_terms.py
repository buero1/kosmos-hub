import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.templates import create_templates
from app.db.base import Base
from app.models.hub_legal_terms import HubLegalTermsRevision
from app.models.hub_pdf_template import HubPdfTemplateRevision
from app.models.hub_user import HubUser
from app.services.hub_legal_terms import HubLegalTermsError, HubLegalTermsService
from app.services.hub_pdf_templates import HubPdfTemplateService


def _admin() -> HubUser:
    return HubUser(username="kosmosadmin", password_hash="hash", role="admin")


def test_default_legal_terms_are_linked_only_to_offer_and_order_templates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        template_service = HubPdfTemplateService(db=db)
        offer = template_service.default_for("offers")
        order = template_service.default_for("orders")
        invoice = template_service.default_for("invoices")
        legal_terms = HubLegalTermsService(db=db).list_terms()

        assert len(legal_terms) == 1
        assert legal_terms[0].name == "Standard-AGB"
        assert offer.legal_terms_id == legal_terms[0].id
        assert order.legal_terms_id == legal_terms[0].id
        assert invoice.legal_terms_id is None
        assert template_service.revision_for(offer).legal_terms_revision_id is not None
        assert template_service.revision_for(order).legal_terms_revision_id is not None
        assert template_service.revision_for(invoice).legal_terms_revision_id is None


def test_updating_legal_terms_sanitizes_html_and_versions_linked_templates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        actor = _admin()
        db.add(actor)
        db.flush()
        template_service = HubPdfTemplateService(db=db)
        offer = template_service.default_for("offers")
        order = template_service.default_for("orders")
        invoice = template_service.default_for("invoices")
        service = HubLegalTermsService(db=db)
        legal_terms = service.list_terms()[0]

        service.update(
            actor=actor,
            legal_terms_id=legal_terms.id,
            name="AGB Websites",
            content_html='<h2>AGB Websites</h2><p>Gültiger Inhalt</p><script>alert("x")</script>',
        )

        revisions = db.scalars(
            select(HubLegalTermsRevision)
            .where(HubLegalTermsRevision.legal_terms_id == legal_terms.id)
            .order_by(HubLegalTermsRevision.version)
        ).all()
        assert legal_terms.version == 2
        assert "<script" not in legal_terms.content_html
        assert [item.version for item in revisions] == [1, 2]
        assert offer.version == 2
        assert order.version == 2
        assert invoice.version == 1
        assert template_service.revision_for(offer).legal_terms_revision_id == revisions[-1].id
        assert template_service.revision_for(order).legal_terms_revision_id == revisions[-1].id


def test_legal_terms_can_be_duplicated_but_linked_or_last_entries_cannot_be_deleted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        actor = _admin()
        db.add(actor)
        db.flush()
        HubPdfTemplateService(db=db).ensure_default_templates()
        service = HubLegalTermsService(db=db)
        original = service.list_terms()[0]
        duplicate = service.duplicate(actor=actor, legal_terms_id=original.id)

        assert duplicate.name == "Standard-AGB (Kopie)"
        with pytest.raises(HubLegalTermsError, match="PDF-Vorlage"):
            service.delete(actor=actor, legal_terms_id=original.id)

        service.delete(actor=actor, legal_terms_id=duplicate.id)
        assert service.get(duplicate.id) is None
        assert db.scalar(select(HubLegalTermsRevision).where(HubLegalTermsRevision.legal_terms_id == duplicate.id))


def test_legal_terms_account_partial_renders_tabs_and_jodit_editor():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubLegalTermsService(db=db)
        selected = service.list_terms()[0]
        request = Request({"type": "http", "method": "GET", "path": "/account", "query_string": b""})
        rendered = create_templates(directory="app/templates").get_template(
            "partials/account_legal_terms.html"
        ).render(
            request=request,
            csrf_token="csrf",
            legal_terms=service.list_terms(),
            selected_legal_terms=selected,
        )

        assert 'id="account-legal-terms"' in rendered
        assert 'class="legal-terms-tabs"' in rendered
        assert 'data-email-rich-editor' in rendered
        assert 'name="content_html"' in rendered
        assert "Standard-AGB" in rendered
