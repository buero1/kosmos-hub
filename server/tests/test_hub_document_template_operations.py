import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.hub_legal_terms import HubLegalTerms, HubLegalTermsRevision
from app.models.hub_pdf_template import HubPdfTemplate, HubPdfTemplateRevision
from app.models.hub_user import HubUser
from app.services.hub_agent import HubAgentService
from app.services.hub_operations import HubOperationError, HubOperationService, get_operation


@pytest.fixture
def hub():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            HubUser(username="admin", password_hash="hash", role="admin"),
            HubUser(username="viewer", password_hash="hash", role="viewer"),
        ])
        db.commit()
        yield HubOperationService(db=db, cipher=None, actor="admin")
    engine.dispose()


def create_pdf(hub):
    return hub.execute("finance.pdf_templates.create", {"name": "Testangebot", "document_type": "offers"}).outputs["template_id"]


def test_read_only_lists_do_not_create_defaults_or_revisions(hub):
    for family in ("pdf_templates", "legal_terms"):
        assert hub.query(f"finance.{family}.list", {}) == {"items": [], "total": 0, "next_offset": ""}
    for model in (HubLegalTerms, HubLegalTermsRevision, HubPdfTemplate, HubPdfTemplateRevision):
        assert not hub.db.scalars(select(model)).all()


@pytest.mark.parametrize("actor", ["viewer", "missing"])
def test_admin_access_is_required_for_reads_and_mutations(hub, actor):
    template_id = create_pdf(hub)
    hub.actor = actor
    with pytest.raises(HubOperationError):
        hub.query("finance.pdf_templates.read", {"template_id": template_id})
    with pytest.raises(HubOperationError):
        hub.query("finance.legal_terms.list", {})
    with pytest.raises(HubOperationError):
        hub.execute("finance.pdf_templates.rename", {"template_id": template_id, "name": "Forbidden"})
    assert hub.db.get(HubPdfTemplate, int(template_id)).name == "Testangebot"


def test_shared_pdf_actions_preserve_partial_updates_and_check_versions(hub):
    template_id = create_pdf(hub)
    original = hub.query("finance.pdf_templates.read", {"template_id": template_id, "block_key": "intro"})
    hub.execute("finance.pdf_templates.update_block", {"template_id": template_id, "block_key": "intro", "is_visible": "false", "expected_version": "1"})
    updated = hub.query("finance.pdf_templates.read", {"template_id": template_id, "block_key": "intro"})
    assert updated["content_html"] == original["content_html"]
    assert updated["is_visible"] is False
    assert updated["version"] == "2"
    with pytest.raises(HubOperationError, match="inzwischen"):
        hub.execute("finance.pdf_templates.rename", {"template_id": template_id, "name": "Old version", "expected_version": "1"})
    hub.execute("finance.pdf_templates.update_block", {"template_id": template_id, "block_key": "intro", "content_html": '<p>Hallo</p><script>alert(1)</script>'})
    updated = hub.query("finance.pdf_templates.read", {"template_id": template_id, "block_key": "intro"})
    assert "<script" not in updated["content_html"]
    assert updated["is_visible"] is False
    hub.execute("finance.pdf_templates.rename", {"template_id": template_id, "name": "Neues Angebot"})
    copy_id = hub.execute("finance.pdf_templates.duplicate", {"template_id": template_id}).outputs["template_id"]
    hub.execute("finance.pdf_templates.set_default", {"template_id": copy_id})
    assert hub.query("finance.pdf_templates.read", {"template_id": copy_id})["is_default"] is True
    hub.execute("finance.pdf_templates.delete", {"template_id": copy_id})
    assert hub.query("finance.pdf_templates.read", {"template_id": template_id})["is_default"] is True
    with pytest.raises(HubOperationError, match="letzte Vorlage"):
        hub.execute("finance.pdf_templates.delete", {"template_id": template_id})


def test_shared_terms_version_linked_templates_and_retain_revisions(hub):
    template_id = create_pdf(hub)
    terms_id = hub.execute("finance.legal_terms.create", {"name": "Test-AGB"}).outputs["legal_terms_id"]
    hub.execute("finance.pdf_templates.set_legal_terms", {"template_id": template_id, "legal_terms_id": terms_id})
    hub.execute("finance.legal_terms.update", {"legal_terms_id": terms_id, "content_html": "<h2>Inhalt</h2><script>bad()</script>", "expected_version": "1"})
    terms = hub.query("finance.legal_terms.read", {"legal_terms_id": terms_id})
    assert terms["name"] == "Test-AGB"
    assert terms["version"] == "2"
    assert "<script" not in terms["content_html"]
    assert hub.query("finance.pdf_templates.read", {"template_id": template_id})["version"] == "3"
    with pytest.raises(HubOperationError, match="PDF-Vorlage"):
        hub.execute("finance.legal_terms.delete", {"legal_terms_id": terms_id})
    copy_id = hub.execute("finance.legal_terms.duplicate", {"legal_terms_id": terms_id}).outputs["legal_terms_id"]
    hub.execute("finance.legal_terms.delete", {"legal_terms_id": copy_id})
    with pytest.raises(HubOperationError, match="nicht gefunden"):
        hub.query("finance.legal_terms.read", {"legal_terms_id": copy_id})
    assert hub.db.scalar(select(HubLegalTermsRevision).where(HubLegalTermsRevision.legal_terms_id == int(copy_id)))
    hub.execute("finance.pdf_templates.set_legal_terms", {"template_id": template_id, "legal_terms_id": ""})
    assert hub.query("finance.pdf_templates.read", {"template_id": template_id})["legal_terms_id"] == ""
    from app.services.hub_pdf_templates import HubPdfTemplateService
    HubPdfTemplateService(db=hub.db).list_templates()
    assert hub.query("finance.pdf_templates.read", {"template_id": template_id})["legal_terms_id"] == ""


def test_positions_share_domain_validation(hub):
    template_id = create_pdf(hub)
    columns = hub.query("finance.pdf_templates.read", {"template_id": template_id})["columns"]
    columns[0]["label"] = "Pos."
    hub.execute("finance.pdf_templates.update_positions", {"template_id": template_id, "columns": json.dumps(columns)})
    assert hub.query("finance.pdf_templates.read", {"template_id": template_id})["columns"][0]["label"] == "Pos."
    columns[0]["width"] = 79
    with pytest.raises(HubOperationError, match="100 Prozent"):
        hub.execute("finance.pdf_templates.update_positions", {"template_id": template_id, "columns": json.dumps(columns)})
    columns[0]["is_enabled"] = "false"
    with pytest.raises(HubOperationError, match="booleschem"):
        hub.execute("finance.pdf_templates.update_positions", {"template_id": template_id, "columns": json.dumps(columns)})
    with pytest.raises(HubOperationError):
        hub.execute("finance.pdf_templates.update_positions", {"template_id": template_id, "columns": "{"})


@pytest.mark.parametrize("extra", [{"unknown": "x"}, {"is_visible": ""}, {"is_visible": "yes"}, {"content_html": "x" * 500001}])
def test_invalid_template_input_is_rejected(hub, extra):
    with pytest.raises(HubOperationError):
        hub.execute("finance.pdf_templates.update_block", {"template_id": create_pdf(hub), "block_key": "intro", **extra})


def test_long_text_and_placeholder_queries_can_be_paged_completely(hub):
    template_id = create_pdf(hub)
    content = "<p>" + "Long text " * 1600 + "</p>"
    hub.execute("finance.pdf_templates.update_block", {"template_id": template_id, "block_key": "intro", "content_html": content})
    result = hub.query("finance.pdf_templates.read", {"template_id": template_id})
    intro = next(item for item in result["blocks"] if item["block_key"] == "intro")
    combined = intro["content_html"]
    offset = intro["next_text_offset"]
    while offset:
        page = hub.query("finance.pdf_templates.read", {"template_id": template_id, "block_key": "intro", "text_offset": offset})
        combined += page["content_html"]
        offset = page["next_text_offset"]
    assert combined == content
    assert hub.query("finance.pdf_templates.placeholders", {"template_id": template_id})["items"]
    for invalid in ("-1", "foo", "9" * 100):
        with pytest.raises(HubOperationError):
            hub.query("finance.pdf_templates.list", {"offset": invalid})


def test_agent_uses_same_operation_and_transactions_remain_with_caller(hub):
    service = object.__new__(HubAgentService)
    service.db, service.cipher = hub.db, None
    result = service._execute_payload(action_type="finance.pdf_templates.create", payload={"input": {"name": "Agent-Test", "document_type": "offers"}}, actor="admin")
    assert hub.db.get(HubPdfTemplate, result["record_id"]).name == "Agent-Test"
    assert get_operation("finance.pdf_templates.create").input_contract()["name"]["required"] is True
    hub.db.rollback()
    assert not hub.db.scalars(select(HubPdfTemplate)).all()
