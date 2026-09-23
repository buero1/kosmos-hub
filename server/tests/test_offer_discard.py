import asyncio
from datetime import datetime, timezone
import json

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select, update

from app.api.routes import web
from app.models.hub_agent import HubAgentAction, HubAgentJob
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_finance_offer import HubFinanceOffer, HubFinanceOfferLine
from app.services.hub_agent import HubAgentService
from app.services.hub_finance import HubFinanceError, HubFinanceService
from app.services.hub_operations import HubOperationError, HubOperationService
from test_hub_finance_operations import env, req
from test_offer_duplicate import source_offer


def duplicate(env, source, service=None):
    result = (service or env.service).execute("finance.offers.duplicate", {"record_id": str(source.id)})
    env.db.commit()
    return env.db.get(HubFinanceOffer, result.record_id)


def test_cancel_after_failed_save_removes_only_copy_and_lines(env):
    source = source_offer(env)
    original_fields = source.encrypted_fields_json
    original_lines = {line.id for line in source.lines}
    copy = duplicate(env, source)
    copy_id = copy.id
    copied_lines = {line.id for line in copy.lines}
    response = asyncio.run(web.update_finance_offer_fields(copy_id, req(env, {
        "customer_id": "", "lead_id": "", "contact_id": "", "offer_field__reference": "Unsaved",
    }), BackgroundTasks(), env.db))
    assert "fields=error" in response.headers["location"]
    assert env.db.get(HubFinanceOffer, copy_id).unassigned_owner_user_id == env.user.id
    response = asyncio.run(web.discard_finance_offer_copy(copy_id, req(env, {}), env.db))
    assert response.status_code == 303 and response.headers["location"] == f"/finance/offers/{source.id}"
    assert env.db.get(HubFinanceOffer, copy_id) is None
    assert not env.db.scalars(select(HubFinanceOfferLine).where(HubFinanceOfferLine.id.in_(copied_lines))).all()
    assert {line.id for line in source.lines} == original_lines
    assert source.encrypted_fields_json == original_fields


def test_own_copy_can_be_discarded_without_permission_to_delete_offers(env):
    source = source_offer(env)
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    env.access.permission(role_key="sales", module_key="finance").can_delete = False
    env.db.commit()
    service = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    copy = duplicate(env, source, service)
    result = service.execute("finance.offers.discard_copy", {"record_id": str(copy.id)})
    assert result.href == f"/finance/offers/{source.id}"
    assert env.db.get(HubFinanceOffer, copy.id) is None


@pytest.mark.parametrize("case", ["saved", "original", "old_orphan", "stale_saved", "linked_with_marker"])
def test_cancel_never_deletes_finalized_or_unrelated_offers(env, case):
    source = source_offer(env)
    copy = duplicate(env, source)
    target = copy
    if case == "saved":
        env.service.execute("finance.offers.update", {"record_id": str(copy.id), "customer_id": str(env.customer.id), "contact_id": str(env.contact.id)})
        assert "_duplicate_source_offer_id" not in HubFinanceService(db=env.db, cipher=env.cipher)._values(copy.encrypted_fields_json)
    elif case == "original":
        target = source
    elif case == "old_orphan":
        copy.unassigned_owner_user_id = None
    elif case == "stale_saved":
        env.db.execute(update(HubFinanceOffer).where(HubFinanceOffer.id == copy.id).values(
            unassigned_owner_user_id=None, customer_id=env.customer.id, contact_id=env.contact.id,
        ).execution_options(synchronize_session=False))
        assert copy.unassigned_owner_user_id is not None
    else:
        copy.customer_id = env.customer.id
    env.db.flush()
    with pytest.raises(HubFinanceError):
        env.service.execute("finance.offers.discard_copy", {"record_id": str(target.id)})
    assert env.db.get(HubFinanceOffer, target.id) is not None


@pytest.mark.parametrize("case", ["foreign", "no_edit", "csrf"])
def test_discard_checks_current_permissions_and_csrf(env, monkeypatch, case):
    source = source_offer(env)
    copy = duplicate(env, source)
    if case == "csrf":
        def deny(*args):
            raise HTTPException(403)
        monkeypatch.setattr(web, "require_csrf", deny)
        with pytest.raises(HTTPException):
            asyncio.run(web.discard_finance_offer_copy(copy.id, req(env, {}), env.db))
    else:
        if case == "no_edit":
            copy.unassigned_owner_user_id = env.sales.id
            env.access.permission(role_key="sales", module_key="finance").can_edit = False
            env.db.commit()
        service = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
        with pytest.raises(HubOperationError):
            service.execute("finance.offers.discard_copy", {"record_id": str(copy.id)})
    assert env.db.get(HubFinanceOffer, copy.id) is not None


@pytest.mark.parametrize("case", ["legacy_copy", "source_deleted", "source_access_revoked"])
def test_discard_returns_to_list_if_original_is_unavailable(env, case):
    source = source_offer(env)
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    env.db.commit()
    service = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    copy = duplicate(env, source, service)
    if case == "legacy_copy":
        domain = HubFinanceService(db=env.db, cipher=env.cipher)
        fields = domain._values(copy.encrypted_fields_json)
        fields.pop("_duplicate_source_offer_id")
        copy.encrypted_fields_json = domain._encrypt(fields)
    elif case == "source_deleted":
        env.service.execute("finance.offers.delete", {"record_id": str(source.id)})
    else:
        env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.user.id, team_id=None)
    env.db.commit()
    result = service.execute("finance.offers.discard_copy", {"record_id": str(copy.id)})
    assert result.href == "/finance/offers"


def test_discard_rollback_restores_copy_lines_and_pdf(env, monkeypatch):
    from app.services.finance_generated_pdf_storage import FinanceGeneratedPdfStorage
    source = source_offer(env)
    copy = duplicate(env, source)
    copy_id = copy.id
    line_ids = {line.id for line in copy.lines}
    pdf = HubFinanceGeneratedPdf(document_type="offers", document_id=copy_id, status="ready", storage_key="test-copy-pdf",
                                template_name="Test", template_version=1, generation_token="test-copy-token", requested_at=datetime.now(timezone.utc))
    env.db.add(pdf)
    env.db.commit()
    removed = []
    monkeypatch.setattr(FinanceGeneratedPdfStorage, "remove", lambda self, key: removed.append(key))
    env.service.execute("finance.offers.discard_copy", {"record_id": str(copy_id)})
    assert not removed
    env.db.rollback()
    assert env.db.get(HubFinanceOffer, copy_id) is not None
    assert {line.id for line in env.db.get(HubFinanceOffer, copy_id).lines} == line_ids
    assert env.db.get(HubFinanceGeneratedPdf, pdf.id) is not None
    env.service.execute("finance.offers.discard_copy", {"record_id": str(copy_id)})
    env.db.commit()
    assert removed == ["test-copy-pdf"]
    assert env.db.scalar(select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.document_id == copy_id)) is None


def test_confirmed_agent_and_ui_share_discard(env):
    source = source_offer(env)
    copy = duplicate(env, source)
    copy_id = copy.id
    job = HubAgentJob(created_by_username=env.user.username, status="ready",
        encrypted_request_json=env.cipher.encrypt("{}"), encrypted_plan_json=env.cipher.encrypt("{}"))
    env.db.add(job)
    env.db.flush()
    action = HubAgentAction(job_id=job.id, sort_order=0, action_type="finance.offers.discard_copy", status="proposed",
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"action_type": "finance.offers.discard_copy", "title": "Discard", "details": "Test",
                                                            "input": {"record_id": str(copy_id)}})))
    env.db.add(action)
    env.db.flush()
    assert env.db.get(HubFinanceOffer, copy_id) is not None
    result = HubAgentService(db=env.db, cipher=env.cipher).execute_action(action_id=action.id, actor=env.user.username)
    assert result.status == "completed" and result.result_href == f"/finance/offers/{source.id}"
    assert env.db.get(HubFinanceOffer, copy_id) is None
