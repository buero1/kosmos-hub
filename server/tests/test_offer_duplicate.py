import asyncio
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select, func
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.models.hub_agent import HubAgentAction, HubAgentJob
from app.models.hub_finance_offer import HubFinanceOffer
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_lead import HubLead
from app.models.hub_user import HubUser
from app.services.hub_agent import HubAgentService
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_operations_shared import require_record
from app.services.hub_operations import HubOperationService, HubOperationError, agent_operations
from scripts.check_hub_contracts import MANIFEST, inventory, validate, validate_catalog, inspect_file
from test_hub_finance_operations import env, req, values


def source_offer(env, *, lead=False):
    data = {**values(env, "offers"), "offer_field__status": "accepted", "offer_field__reference": "Keep reference",
            "offer_field__offer_date": "2026-07-15", "offer_field__valid_until": "2026-08-15",
            "offer_field__payment_terms": "30_days", "offer_field__currency": "CHF",
            "offer_line__0__description": "Original service description", "offer_line__0__discount_percent": "12.5"}
    if lead:
        row = HubLead(encrypted_profile_json=env.cipher.encrypt(json.dumps({"fields": {"company": "Lead", "last_name": "Lead"}})))
        env.db.add(row)
        env.db.flush()
        data.update(customer_id="", contact_id="", lead_id=str(row.id))
    result = env.service.execute("finance.offers.create", data)
    row = env.db.get(HubFinanceOffer, result.record_id)
    row.zoho_books_id = "old-external-id"
    row.zoho_imported_at = datetime(2026, 7, 10)
    row.zoho_modified_at = datetime(2026, 7, 14)
    row.offer_number = "EST-123456"
    env.db.commit()
    return row


@pytest.mark.parametrize("lead", [False, True])
def test_copy_snapshots_new_identity_and_empty_links_without_pdf(env, lead):
    source = source_offer(env, lead=lead)
    domain = HubFinanceService(db=env.db, cipher=env.cipher)
    source_fields = domain._values(source.encrypted_fields_json)
    source_lines = [(line.id, line.article_id, line.position_index, line.encrypted_fields_json) for line in source.lines]
    pdf_count = env.db.scalar(select(func.count()).select_from(HubFinanceGeneratedPdf))
    result = env.service.execute("finance.offers.duplicate", {"record_id": str(source.id)})
    copy = env.db.get(HubFinanceOffer, result.record_id)
    assert copy.offer_number == f"ANG-{copy.id:06d}" and copy.id != source.id
    assert result.href == f"/finance/offers/{copy.id}?edit=true" and not result.background_token
    assert (copy.customer_id, copy.lead_id, copy.contact_id, copy.zoho_books_id, copy.zoho_modified_at, copy.zoho_imported_at) == (None,) * 6
    assert copy.unassigned_owner_user_id == env.user.id
    assert copy.pdf_template_id == source.pdf_template_id
    assert domain._values(copy.encrypted_fields_json) == {**source_fields, "status": "draft", "_duplicate_source_offer_id": str(source.id)}
    assert [(line.article_id, line.position_index, domain._values(line.encrypted_fields_json)) for line in copy.lines] == [
        (line.article_id, line.position_index, domain._values(line.encrypted_fields_json)) for line in source.lines]
    assert not {line.id for line in copy.lines} & {line.id for line in source.lines}
    assert source_lines == [(line.id, line.article_id, line.position_index, line.encrypted_fields_json) for line in source.lines]
    assert domain._values(source.encrypted_fields_json) == source_fields
    assert env.db.scalar(select(func.count()).select_from(HubFinanceGeneratedPdf)) == pdf_count


def test_ui_and_confirmed_agent_use_same_operation(env):
    source = source_offer(env)
    response = asyncio.run(web.duplicate_finance_offer(source.id, req(env, {}), env.db))
    assert response.status_code == 303 and response.headers["location"].endswith("?edit=true")
    job = HubAgentJob(created_by_username=env.user.username, status="ready",
        encrypted_request_json=env.cipher.encrypt("{}"), encrypted_plan_json=env.cipher.encrypt("{}"))
    env.db.add(job)
    env.db.flush()
    action = HubAgentAction(job_id=job.id, sort_order=0, action_type="finance.offers.duplicate", status="proposed",
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"action_type": "finance.offers.duplicate", "title": "Duplicate", "details": "Test",
                                                            "input": {"record_id": str(source.id)}})))
    env.db.add(action)
    env.db.flush()
    assert env.db.scalar(select(func.count()).select_from(HubFinanceOffer)) == 2
    result = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor=env.user.username)
    assert result.status == "completed" and result.result_href.endswith("?edit=true")
    copies = env.db.scalars(select(HubFinanceOffer).where(HubFinanceOffer.id != source.id)).all()
    assert len(copies) == 2 and len({row.offer_number for row in copies}) == 2
    assert all(row.customer_id is None and row.lead_id is None for row in copies)
    assert "finance.offers.duplicate" in {operation.key for operation in agent_operations()}


def test_private_copy_then_new_link_uses_normal_permissions(env):
    source = source_offer(env)
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        limited.execute("finance.offers.duplicate", {"record_id": str(source.id)})
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    env.db.commit()
    result = limited.execute("finance.offers.duplicate", {"record_id": str(source.id)})
    copy = require_record(limited, "offers", result.record_id, "edit")
    other = HubUser(username="other-sales", password_hash="x", role="sales")
    env.db.add(other)
    env.db.flush()
    foreign = HubOperationService(db=env.db, cipher=env.cipher, actor=other.username)
    with pytest.raises(HubOperationError):
        require_record(foreign, "offers", copy.id)
    assert str(copy.id) in {str(row["record_id"]) for row in limited.query("finance.list", {"kind": "offers"})["items"]}
    limited.execute("finance.offers.update", {"record_id": str(copy.id), "customer_id": str(env.customer.id), "contact_id": str(env.contact.id),
                                             "offer_line__0__name": "Changed copy"})
    assert copy.unassigned_owner_user_id is None and copy.customer_id == env.customer.id
    assert env.cipher.decrypt(source.lines[0].encrypted_fields_json) != env.cipher.decrypt(copy.lines[0].encrypted_fields_json)
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=other.id, team_id=None)
    env.db.flush()
    with pytest.raises(HubOperationError):
        require_record(limited, "offers", copy.id)


@pytest.mark.parametrize("missing", ["create", "edit"])
def test_duplicate_requires_create_and_edit_permissions(env, missing):
    source = source_offer(env)
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    permission = env.access.permission(role_key="sales", module_key="finance")
    setattr(permission, "can_" + missing, False)
    env.db.commit()
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        limited.execute("finance.offers.duplicate", {"record_id": str(source.id)})
    assert env.db.scalar(select(func.count()).select_from(HubFinanceOffer)) == 1


@pytest.mark.parametrize("data", [{}, {"record_id": ""}, {"record_id": "9999"}, {"record_id": "bad"}, {"record_id": 1}, {"record_id": "1", "customer_id": "1"}])
def test_invalid_duplicate_creates_nothing(env, data):
    source_offer(env)
    with pytest.raises(HubOperationError):
        env.service.execute("finance.offers.duplicate", data)
    assert env.db.scalar(select(func.count()).select_from(HubFinanceOffer)) == 1


def test_ui_duplicate_requires_csrf_and_rolls_back_failures(env, monkeypatch):
    source = source_offer(env)
    from fastapi import HTTPException
    def deny(*args):
        raise HTTPException(403)
    monkeypatch.setattr(web, "require_csrf", deny)
    with pytest.raises(HTTPException):
        asyncio.run(web.duplicate_finance_offer(source.id, req(env, {}), env.db))
    assert env.db.scalar(select(func.count()).select_from(HubFinanceOffer)) == 1


def test_copy_preserves_article_snapshot_and_outer_rollback(env):
    source = source_offer(env)
    article = env.service.execute("finance.articles.create", values(env, "articles"))
    env.service.execute("finance.offers.update", {"record_id": str(source.id), "offer_line__0__article_id": str(article.record_id),
        "offer_line__0__name": "Custom snapshot", "offer_line__0__unit": "Monatlich", "offer_line__0__sku": "SNAPSHOT"})
    env.db.commit()
    with pytest.raises(RuntimeError):
        with env.db.begin_nested():
            result = env.service.execute("finance.offers.duplicate", {"record_id": str(source.id)})
            copy = env.db.get(HubFinanceOffer, result.record_id)
            assert copy.lines[0].article_id == article.record_id
            snapshot = json.loads(env.cipher.decrypt(copy.lines[0].encrypted_fields_json))
            assert snapshot["name"] == "Custom snapshot" and snapshot["unit"] == "Monatlich" and snapshot["sku"] == "SNAPSHOT"
            raise RuntimeError("Later failure in enclosing action")
    assert env.db.scalar(select(func.count()).select_from(HubFinanceOffer)) == 1


def test_offer_editor_template_and_export_browser_fixtures(env):
    source = source_offer(env)
    app = create_app()
    def render(row, query=b""):
        request = Request({"type": "http", "method": "GET", "path": f"/finance/offers/{row.id}", "query_string": query,
            "headers": [], "scheme": "http", "server": ("hub.test", 80), "session": {}, "app": app, "router": app.router})
        request.state.hub_user = env.user
        return web.finance_offer_detail_page(row.id, request, env.db).body.decode()
    original = render(source)
    assert original.index('>Duplizieren<') < original.index('data-finance-delete-open')
    assert 'data-customer-edit-start-open hidden' not in original
    result = env.service.execute("finance.offers.duplicate", {"record_id": str(source.id)})
    copy = env.db.get(HubFinanceOffer, result.record_id)
    duplicate = render(copy, b"edit=true")
    assert 'data-customer-edit-start-open hidden' in duplicate
    assert 'name="customer_id" value=""' in duplicate and 'name="lead_id" value=""' in duplicate
    assert 'id="finance-offer-discard-form"' in duplicate
    assert 'form="finance-offer-discard-form" data-customer-edit-discard' in duplicate
    env.service.execute("finance.offers.update", {"record_id": str(copy.id), "customer_id": str(env.customer.id), "contact_id": str(env.contact.id)})
    saved = render(copy)
    assert 'id="finance-offer-discard-form"' not in saved
    assert 'id="finance-offer-discard-form"' not in original
    directory = os.environ.get("HUB_TEST_ARTIFACT_DIR")
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        for name, content in (("original", original), ("duplicate", duplicate), ("saved", saved)):
            (path / f"{name}.html").write_text(content, encoding="utf-8")
        (path / "data.json").write_text(json.dumps({"source": source.id, "copy": copy.id, "customer": env.customer.id, "contact": env.contact.id}), encoding="utf-8")


@pytest.mark.parametrize("handler,path,key,method", [
    ("duplicate_finance_offer", "duplicate", "finance.offers.duplicate", "duplicate_offer"),
    ("discard_finance_offer_copy", "discard-copy", "finance.offers.discard_copy", "discard_offer_copy"),
])
def test_architecture_rejects_missing_contract_and_boundary_for_actual_duplicate_route(handler, path, key, method):
    routes, files = inventory()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    identity = f"app/api/routes/web.py:{handler}:POST:/finance/offers/{{offer_id}}/{path}"
    missing = deepcopy(manifest)
    missing["routes"].pop(identity, None)
    assert any("NEW route" in error and identity in error for error in validate(routes, files, missing))
    assert manifest["routes"][identity]["contracts"] == [key]
    assert manifest["routes"][identity]["reader_method"] == method
    assert not [error for error in validate(routes, files, manifest) if identity in error]
    assert not [error for error in validate_catalog(routes, manifest) if identity in error]
    source = Path("app/api/routes/web.py").read_text(encoding="utf-8-sig")
    bypassed = source.replace('_finance_gateway(request, db).execute("' + key + '", {"record_id": str(offer_id)})', 'db.add(object())')
    changed, _ = inspect_file(bypassed, "app/api/routes/web.py")
    bypass_manifest = deepcopy(manifest)
    bypass_manifest["routes"][identity]["fingerprint"] = changed[identity]["fingerprint"]
    assert any("boundary bypassed" in error and identity in error for error in validate({**routes, identity: changed[identity]}, files, bypass_manifest))
