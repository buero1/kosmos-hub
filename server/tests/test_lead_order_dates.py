"""Order dates follow the actual result transition, not unrelated later edits."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.services import hub_workflows
from app.services.hub_leads import HubLeadService
from app.services.hub_workflows import HubWorkflowService, LEAD_RESULT_FIELD_UPDATE_WORKFLOW_KEY


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc), "2026-09-25"),
    (datetime(2026, 9, 25, 22, 30, tzinfo=timezone.utc), "2026-09-26"),
    (datetime(2026, 1, 31, 23, 30, tzinfo=timezone.utc), "2026-02-01"),
    (datetime(2026, 3, 28, 23, 30, tzinfo=timezone.utc), "2026-03-29"),
    (datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc), "2026-10-25"),
    (datetime(2026, 9, 25, 23, 30), "2026-09-25"),
])
@pytest.mark.parametrize("existing_date", ["", "2020-01-01"])
def test_transition_stamps_both_dates_with_berlin_change_date(db, now, expected, existing_date):
    values = {"lead_result": "Vertrag", "order_date": existing_date, "billing_result_date": existing_date}
    HubWorkflowService(db=db).apply_lead_field_updates(previous_values={"lead_result": "Offen"}, updated_values=values, now=now)
    assert values["order_date"] == values["billing_result_date"] == expected
    assert values["lead_status"] == "Umgewandelt"
    assert values["billing_result"] == "Auftrag"


@pytest.mark.parametrize("existing_date", ["", "2020-01-01"])
def test_saving_existing_order_does_not_backfill_or_overwrite_dates(db, existing_date):
    values = {"lead_result": "Vertrag", "order_date": existing_date, "billing_result_date": existing_date}
    HubWorkflowService(db=db).apply_lead_field_updates(previous_values=dict(values), updated_values=values,
                                                     now=datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert values["order_date"] == values["billing_result_date"] == existing_date


def test_disabled_workflow_does_not_stamp_dates(db):
    service = HubWorkflowService(db=db)
    workflow = next(item for item in service.list_workflows() if item.workflow_key == LEAD_RESULT_FIELD_UPDATE_WORKFLOW_KEY)
    workflow.is_enabled = False
    db.flush()
    values = {"lead_result": "Vertrag", "order_date": "2020-01-01", "billing_result_date": ""}
    service.apply_lead_field_updates(previous_values={"lead_result": "Offen"}, updated_values=values)
    assert values["order_date"] == "2020-01-01"
    assert values["billing_result_date"] == ""


@pytest.mark.parametrize("result", ["Offen", "Stattgefunden", "Stattgefunden + Auftrag", "Storniert", "Rücktritt", "Kein Auftrag"])
def test_other_results_keep_existing_date_behavior(db, result):
    values = {"lead_result": result, "order_date": "2020-01-01", "billing_result_date": "2020-01-02"}
    HubWorkflowService(db=db).apply_lead_field_updates(previous_values={"lead_result": "Vertrag"}, updated_values=values)
    assert values["order_date"] == "2020-01-01"
    assert values["billing_result_date"] == "2020-01-02"


@pytest.mark.parametrize("path", ["create", "update", "external"])
def test_dates_are_persisted_through_shared_lead_service(db, monkeypatch, path):
    class Clock(datetime):
        current = datetime(2026, 9, 25, 22, 30, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(hub_workflows, "datetime", Clock)
    service = HubLeadService(db=db, cipher=SecretCipher("a" * 32))
    if path == "external":
        lead, _ = service.upsert_external_lead(source_system="test", source_external_id="order-date",
                                              field_values={"last_name": "Example", "lead_result": "Offen"})
        service.upsert_external_lead(source_system="test", source_external_id="order-date", field_values={"lead_result": "Vertrag"})
    else:
        lead = service.create_lead(submitted_values={"lead_field__last_name": "Example", "lead_field__lead_result": "Vertrag" if path == "create" else "Offen"})
        if path == "update":
            service.update_lead(lead_id=lead.id, submitted_values={"lead_field__lead_result": "Vertrag"})
    db.commit()
    db.expire_all()

    def dates():
        values = {field.key: field.form_value for field in service.get_detail(lead_id=lead.id).fields}
        return values["order_date"], values["billing_result_date"]

    assert dates() == ("2026-09-26", "2026-09-26")
    Clock.current = datetime(2026, 10, 1, tzinfo=timezone.utc)
    service.update_lead(lead_id=lead.id, submitted_values={"lead_field__company": "New company name"})
    assert dates() == ("2026-09-26", "2026-09-26")
    service.update_lead(lead_id=lead.id, submitted_values={"lead_field__lead_result": "Rücktritt"})
    service.update_lead(lead_id=lead.id, submitted_values={"lead_field__lead_result": "Vertrag"})
    assert dates() == ("2026-10-01", "2026-10-01")
