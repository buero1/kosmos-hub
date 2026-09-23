from inspect import getsource
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes import web
from app.api.routes.web import router
from app.core.security import SecretCipher
from app.core.templates import create_templates
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.module_layout_catalog import module_layout_definitions


TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_global_layout_catalog_covers_every_module_with_unique_fields():
    definitions = module_layout_definitions()

    assert {definition.layout_key for definition in definitions} == {
        "customer-fields",
        "contact-fields",
        "lead-fields",
        "case-fields",
        "finance-article-fields",
        "finance-offer-fields",
        "finance-order-fields",
        "finance-invoice-fields",
        "finance-dunning-fields",
        "finance-recurring-invoice-fields",
    }
    for definition in definitions:
        keys = tuple(field.key for field in definition.fields)
        assert keys
        assert len(keys) == len(set(keys))


def test_global_layout_editor_has_get_and_post_routes():
    route_methods = {
        (route.path, method)
        for route in router.routes
        for method in (route.methods or ())
    }

    assert ("/module-layouts/{layout_key}", "GET") in route_methods
    assert ("/module-layouts/{layout_key}", "POST") in route_methods


def test_directory_pages_expose_layout_menu_and_detail_pages_do_not():
    directory_templates = (
        "customers.html",
        "contacts.html",
        "leads.html",
        "cases.html",
        "finance_articles.html",
        "finance_offers.html",
        "finance_documents.html",
    )
    detail_templates = (
        "customer_detail.html",
        "customer_contact_detail.html",
        "lead_detail.html",
        "case_detail.html",
        "finance_article_detail.html",
        "finance_offer_detail.html",
        "finance_document_detail.html",
    )

    for name in directory_templates:
        source = (TEMPLATE_ROOT / name).read_text(encoding="utf-8")
        assert "module_layout_menu" in source
    for name in detail_templates:
        source = (TEMPLATE_ROOT / name).read_text(encoding="utf-8")
        assert "data-layout-edit-open" not in source


def test_leads_directory_layout_menu_uses_a_route_supplied_flag():
    route = next(route for route in router.routes if route.path == "/leads" and "GET" in route.methods)
    source = (TEMPLATE_ROOT / "leads.html").read_text(encoding="utf-8")
    assert '"can_manage_lead_layout": user.role == "admin"' in getsource(route.endpoint)
    assert "can_manage_lead_layout" in source
    assert "user.role" not in source


def test_leads_directory_renders_for_admin_without_undefined_user(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web, "templates", create_templates(directory=str(TEMPLATE_ROOT)))
    monkeypatch.setattr(web, "get_secret_cipher", lambda: SecretCipher("a" * 32))
    with Session(engine) as db:
        user = HubUser(username="admin", password_hash="x", role="admin")
        db.add(user)
        db.flush()
        request = Request({"type": "http", "method": "GET", "path": "/leads", "headers": [], "session": {}})
        request.state.hub_user = user

        response = web.leads_page(request, db)

        assert response.status_code == 200
        assert b"Layout bearbeiten" in response.body


def test_global_layout_page_starts_in_drag_and_drop_mode():
    source = (TEMPLATE_ROOT / "module_layout_edit.html").read_text(encoding="utf-8")
    base_source = (TEMPLATE_ROOT / "base.html").read_text(encoding="utf-8")

    assert "data-layout-editor-start-open" in source
    assert "show_more_layout_item()" in source
    assert "if (startsOpen) { setLayoutEditing(true); }" in base_source
