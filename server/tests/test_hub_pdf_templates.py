import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.templates import create_templates
from app.db.base import Base
from app.models.hub_pdf_template import HubPdfTemplateRevision
from app.models.hub_user import HubUser
from app.services.hub_pdf_templates import HubPdfTemplateError, HubPdfTemplateService


def _admin() -> HubUser:
    return HubUser(username="kosmosadmin", password_hash="hash", role="admin")


def test_pdf_template_service_seeds_one_versioned_default_per_finance_document_type():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubPdfTemplateService(db=db)
        service.ensure_default_templates()

        templates = service.list_templates()
        revisions = db.scalars(select(HubPdfTemplateRevision)).all()

        assert [(item.document_type, item.is_default, item.version) for item in templates] == [
            ("dunnings", True, 1),
            ("invoices", True, 1),
            ("offers", True, 1),
            ("orders", True, 1),
        ]
        assert len(revisions) == 4
        assert all(len(service.editor_view(item).blocks) == 7 for item in templates)
        assert [item.legal_terms_id is not None for item in templates] == [False, False, True, True]


def test_pdf_template_block_update_is_sanitized_and_creates_an_immutable_revision():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        actor = _admin()
        db.add(actor)
        db.flush()
        service = HubPdfTemplateService(db=db)
        template = service.default_for("orders")

        service.update_block(
            actor=actor,
            template_id=template.id,
            block_key="intro",
            content_html='<p>Hallo ${customerName}</p><script>alert("x")</script>',
            is_visible=True,
        )

        assert template.version == 2
        assert "<script" not in template.content_json
        assert "${customerName}" in template.content_json
        assert [revision.version for revision in template.revisions] == [1, 2]
        assert "Hallo Schreinerei Muster" in service.editor_view(template).blocks[4].preview_html


def test_pdf_template_table_configuration_requires_complete_hundred_percent_layout():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        actor = _admin()
        db.add(actor)
        db.flush()
        service = HubPdfTemplateService(db=db)
        template = service.default_for("invoices")
        columns = json.loads(template.content_json)["positions"]["columns"]
        columns[0]["width"] = 5

        with pytest.raises(HubPdfTemplateError, match="100 Prozent"):
            service.update_positions(actor=actor, template_id=template.id, columns=columns)


def test_pdf_templates_can_be_duplicated_promoted_and_deleted_but_not_removed_entirely():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        actor = _admin()
        db.add(actor)
        db.flush()
        service = HubPdfTemplateService(db=db)
        original = service.default_for("offers")
        duplicate = service.duplicate(actor=actor, template_id=original.id)

        assert duplicate.name == "Standardangebot (Kopie)"
        assert duplicate.version == 1
        assert not duplicate.is_default

        service.set_default(actor=actor, template_id=duplicate.id)
        assert duplicate.is_default
        assert not original.is_default

        service.delete(actor=actor, template_id=duplicate.id)
        assert original.is_default
        with pytest.raises(HubPdfTemplateError, match="letzte Vorlage"):
            service.delete(actor=actor, template_id=original.id)


def test_pdf_template_account_partial_renders_a4_blocks_jodit_drawer_and_table_editor():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubPdfTemplateService(db=db)
        selected = service.editor_view(service.default_for("orders"))
        templates_by_type = {
            definition.key: service.list_templates(document_type=definition.key)
            for definition in service_type_definitions()
        }
        request = Request({"type": "http", "method": "GET", "path": "/account", "query_string": b""})
        rendered = create_templates(directory="app/templates").get_template(
            "partials/account_pdf_templates.html"
        ).render(
            request=request,
            csrf_token="csrf",
            pdf_template_types=service_type_definitions(),
            pdf_templates_by_type=templates_by_type,
            selected_pdf_template=selected,
            selected_pdf_template_type="orders",
            pdf_line_sources=line_sources(),
            legal_terms=(selected.legal_terms,),
        )

        assert 'data-pdf-template-preview-block="sender"' in rendered
        assert 'data-pdf-template-open-positions' in rendered
        assert 'data-email-rich-editor' in rendered
        assert 'data-template-placeholder-source' in rendered
        assert 'data-template-placeholder-search' in rendered
        assert 'data-token="${Order.Number}"' in rendered
        assert 'data-token="${Contact.Name}"' in rendered
        assert 'data-token="${Company.Name}"' in rendered
        assert 'data-token="${Invoice.Number}"' not in rendered
        assert 'data-pdf-template-position-form' in rendered
        assert "Schreinerei Muster" in rendered
        assert "Allgemeine Geschäftsbedingungen" in rendered
        assert 'name="legal_terms_id"' in rendered


def test_invoice_template_preview_has_no_legal_terms_control_or_page():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubPdfTemplateService(db=db)
        selected = service.editor_view(service.default_for("invoices"))
        templates_by_type = {
            definition.key: service.list_templates(document_type=definition.key)
            for definition in service_type_definitions()
        }
        request = Request({"type": "http", "method": "GET", "path": "/account", "query_string": b""})
        rendered = create_templates(directory="app/templates").get_template(
            "partials/account_pdf_templates.html"
        ).render(
            request=request,
            csrf_token="csrf",
            pdf_template_types=service_type_definitions(),
            pdf_templates_by_type=templates_by_type,
            selected_pdf_template=selected,
            selected_pdf_template_type="invoices",
            pdf_line_sources=line_sources(),
            legal_terms=(),
        )

        assert 'name="legal_terms_id"' not in rendered
        assert "pdf-template-legal-page" not in rendered


def service_type_definitions():
    from app.services.hub_pdf_templates import PDF_TEMPLATE_TYPES

    return PDF_TEMPLATE_TYPES


def line_sources():
    from app.services.hub_pdf_templates import PDF_LINE_SOURCES

    return PDF_LINE_SOURCES
