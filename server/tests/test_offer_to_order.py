"""A conversion copies immutable line snapshots; the order is completed by its user."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, event
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.models.hub_finance_documents import HubFinanceOrder
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import HubFinanceDocumentService, ORDER_MODULE
from app.services.hub_finance_operations_shared import finance_options, require_record
from app.services.hub_operations import HubOperationError, HubOperationService
from scripts.check_hub_contracts import MANIFEST, inventory, validate, validate_catalog
from test_hub_finance_operations import env, req, values
from test_offer_duplicate import source_offer

KEY = "finance.offers.convert_to_order"


def convert(env, source, service=None):
    return (service or env.service).execute(KEY, {"record_id": str(source.id)})


def count(env, model):
    return env.db.scalar(select(func.count()).select_from(model))


def complete_values(env, order):
    return {key: value for key, value in values(env, "orders").items() if not key.startswith("document_line__")} | {
        "record_id": str(order.id), "document_field__order_date": "2026-09-25",
    }


@pytest.mark.parametrize("lead", [False, True])
def test_conversion_copies_only_positions_and_currency_and_keeps_source(env, lead):
    source = source_offer(env, lead=lead)
    before = (source.encrypted_fields_json, source.updated_at, source.customer_id, source.contact_id,
              source.offer_number, source.zoho_books_id, source.pdf_template_id)
    lines = [(line.id, line.article_id, line.position_index, line.encrypted_fields_json) for line in source.lines]
    pdfs, emails = count(env, HubFinanceGeneratedPdf), count(env, HubMailboxEmail)
    result = convert(env, source)
    order = env.db.get(HubFinanceOrder, result.record_id)
    env.db.commit()
    env.db.expire_all()
    assert result.href == f"/finance/orders/{order.id}?edit=true" and not result.background_token
    assert order.order_number == f"AUF-{order.id:06d}"
    assert order.offer_id == source.id and order.unassigned_owner_user_id == env.user.id
    assert (order.customer_id, order.contact_id, order.pdf_template_id, order.zoho_books_id, order.zoho_crm_id) == (None,) * 5
    fields = json.loads(env.cipher.decrypt(order.encrypted_fields_json))
    assert fields["status"] == "draft" and fields["currency"] == "CHF"
    assert set(fields) == {"status", "currency", "created_time", "modified_time"}
    assert [(line.article_id, line.position_index, line.encrypted_fields_json) for line in order.lines] == [line[1:] for line in lines]
    assert lines == [(line.id, line.article_id, line.position_index, line.encrypted_fields_json) for line in source.lines]
    assert before == (source.encrypted_fields_json, source.updated_at, source.customer_id, source.contact_id,
                      source.offer_number, source.zoho_books_id, source.pdf_template_id)
    assert count(env, HubFinanceGeneratedPdf) == pdfs and count(env, HubMailboxEmail) == emails
    assert convert(env, source).record_id == order.id and count(env, HubFinanceOrder) == 1
    env.service.execute("finance.orders.update", complete_values(env, order))
    assert order.unassigned_owner_user_id is None and order.customer_id == env.customer.id
    assert order.offer_id == source.id
    assert json.loads(env.cipher.decrypt(order.encrypted_fields_json))["currency"] == "CHF"
    assert before[0] == source.encrypted_fields_json


def test_article_snapshots_descriptions_decimals_and_independent_edits(env):
    source = source_offer(env)
    article = env.service.execute("finance.articles.create", values(env, "articles"))
    env.service.execute("finance.offers.update", {
        "record_id": str(source.id), "offer_line__0__article_id": str(article.record_id),
        "offer_line__0__name": "Custom article", "offer_line__0__sku": "Custom SKU",
        "offer_line__0__quantity": "2.75", "offer_line__0__unit": "Monatlich",
        "offer_line__0__unit_price": "19.95", "offer_line__0__discount_percent": "12.5",
        "offer_line__0__tax_rate": "7", "offer_line__0__description": "Line one\nLine two <test>",
    })
    env.service.execute("finance.articles.update", {"record_id": str(article.record_id), "article_field__net_price": "999"})
    env.db.commit()
    result = convert(env, source)
    order = env.db.get(HubFinanceOrder, result.record_id)
    document = HubFinanceDocumentService(db=env.db, cipher=env.cipher).get_detail(module=ORDER_MODULE, document_id=order.id)
    original = HubFinanceService(db=env.db, cipher=env.cipher).get_offer_detail(offer_id=source.id)
    assert document.totals == original.totals
    copied = json.loads(env.cipher.decrypt(order.lines[0].encrypted_fields_json))
    assert copied["description"] == "Line one\nLine two <test>"
    assert copied["quantity"] == "2.75" and copied["unit_price"] == "19.95"
    env.service.execute("finance.orders.update", complete_values(env, order) | {"document_line__0__description": "Order only"})
    assert json.loads(env.cipher.decrypt(source.lines[0].encrypted_fields_json))["description"] == copied["description"]
    assert json.loads(env.cipher.decrypt(order.lines[0].encrypted_fields_json))["description"] == "Order only"


@pytest.mark.parametrize("missing", ["view", "create", "edit"])
def test_permissions_are_required(env, missing):
    source = source_offer(env)
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    setattr(env.access.permission(role_key="sales", module_key="finance"), "can_" + missing, False)
    env.db.commit()
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        convert(env, source, limited)
    assert count(env, HubFinanceOrder) == 0


def test_customer_scope_private_draft_and_final_assignment(env):
    source = source_offer(env)
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        convert(env, source, limited)
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    env.db.commit()
    order_id = convert(env, source, limited).record_id
    order = require_record(limited, "orders", order_id, "edit")
    other = HubUser(username="other-sales", password_hash="x", role="sales")
    env.db.add(other)
    env.db.flush()
    foreign = HubOperationService(db=env.db, cipher=env.cipher, actor=other.username)
    assert foreign.query("finance.list", {"kind": "orders"})["items"] == []
    for action in ("view", "edit", "delete"):
        with pytest.raises(HubOperationError):
            require_record(foreign, "orders", order.id, action)
    assert require_record(env.service, "orders", order.id).id == order.id
    limited.execute("finance.orders.update", complete_values(env, order))
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=other.id, team_id=None)
    env.db.flush()
    with pytest.raises(HubOperationError):
        require_record(limited, "orders", order.id)
    assert require_record(foreign, "orders", order.id).id == order.id


@pytest.mark.parametrize("data", [{}, {"record_id": ""}, {"record_id": "bad"}, {"record_id": "9999"}, {"record_id": 1}, {"record_id": "1", "customer_id": "1"}])
def test_bad_inputs_create_nothing(env, data):
    source_offer(env)
    with pytest.raises(HubOperationError):
        env.service.execute(KEY, data)
    assert count(env, HubFinanceOrder) == 0


def test_route_csrf_redirect_and_rollback(env, monkeypatch):
    source = source_offer(env)
    def deny(*args):
        raise HTTPException(403)
    monkeypatch.setattr(web, "require_csrf", deny)
    with pytest.raises(HTTPException):
        asyncio.run(web.convert_finance_offer_to_order(source.id, req(env, {}), env.db))
    assert count(env, HubFinanceOrder) == 0
    monkeypatch.setattr(web, "require_csrf", lambda *args: None)
    response = asyncio.run(web.convert_finance_offer_to_order(source.id, req(env, {}), env.db))
    assert response.status_code == 303 and response.headers["location"].endswith("?edit=true")
    assert count(env, HubFinanceOrder) == 1
    response = asyncio.run(web.convert_finance_offer_to_order(9999, req(env, {}), env.db))
    assert "fields=error" in response.headers["location"] and count(env, HubFinanceOrder) == 1


def test_outer_rollback_and_failed_line_insert_leave_no_order(env):
    source = source_offer(env)
    convert(env, source)
    env.db.rollback()
    assert count(env, HubFinanceOrder) == 0
    def fail(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO hub_finance_order_lines"):
            raise RuntimeError("Injected failure")
    event.listen(env.db.bind, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            convert(env, source)
    finally:
        event.remove(env.db.bind, "before_cursor_execute", fail)
    env.db.commit()
    assert count(env, HubFinanceOrder) == 0


def test_editor_template_and_browser_fixtures(env):
    source = source_offer(env)
    app = create_app()
    def request(path, query=b""):
        req = Request({"type": "http", "method": "GET", "path": path, "query_string": query,
                       "headers": [], "scheme": "http", "server": ("hub.test", 80), "session": {}, "app": app, "router": app.router})
        req.state.hub_user = env.user
        return req
    original = web.finance_offer_detail_page(source.id, request(f"/finance/offers/{source.id}"), env.db).body.decode()
    assert '>In Auftrag umwandeln<' in original
    assert f'action="/finance/offers/{source.id}/convert-to-order"' in original
    order_id = convert(env, source).record_id
    draft = web.finance_document_detail_page("orders", order_id, request(f"/finance/orders/{order_id}", b"edit=true"), env.db).body.decode()
    assert 'data-customer-edit-start-open hidden' in draft
    assert 'name="document_field__order_name" value=""' in draft
    assert 'name="document_field__order_date" value=""' in draft
    assert 'data-finance-document-currency="CHF"' in draft
    directory = os.environ.get("HUB_ORDER_CONVERSION_TEST_ARTIFACT_DIR")
    if directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        for name, content in (("original", original), ("draft", draft)):
            (root / f"{name}.html").write_text(content, encoding="utf-8")
        (root / "data.json").write_text(json.dumps({"source": source.id, "order": order_id, "customer": env.customer.id, "contact": env.contact.id}), encoding="utf-8")


def test_route_architecture_contract():
    routes, files = inventory()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    identity = "app/api/routes/web.py:convert_finance_offer_to_order:POST:/finance/offers/{offer_id}/convert-to-order"
    assert manifest["routes"][identity]["contracts"] == [KEY]
    assert not [error for error in validate(routes, files, manifest) + validate_catalog(routes, manifest) if identity in error]
    missing = deepcopy(manifest)
    missing["routes"].pop(identity)
    assert any(identity in error for error in validate(routes, files, missing))
