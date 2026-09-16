from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.activity_tracking import _changed_fields, _module_for_object, install_activity_tracking
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_activity_event import HubActivityEvent
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.services.audit import write_audit_log
from app.services.hub_activity import (
    begin_activity_request,
    end_activity_request,
    list_activity_events,
    module_and_resource_for_path,
    record_http_activity,
)


def _session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    install_activity_tracking(factory)
    return factory


def test_background_changes_are_batched_by_module():
    factory = _session_factory()
    with factory() as db:
        db.add_all([Customer(name="One"), Customer(name="Two")])
        db.commit()
        events = db.scalars(select(HubActivityEvent)).all()
        assert len(events) == 1
        assert events[0].module_key == "customers"
        assert events[0].category == "bulk"
        assert events[0].actor == "system"
        assert events[0].resource_id is None


def test_manual_audit_is_not_duplicated_by_change_tracking():
    factory = _session_factory()
    token = begin_activity_request("admin", "/customers/new", "POST")
    try:
        with factory() as db:
            customer = Customer(name="Beispiel")
            db.add(customer)
            db.flush()
            write_audit_log(
                db, site=None, actor="admin", source="hub-web", action="create-customer",
                result="ok", detail=f"Created Customer {customer.id}.",
            )
            db.commit()
            events = db.scalars(select(HubActivityEvent)).all()
            assert len(events) == 1
            assert events[0].resource_id == str(customer.id)
            assert events[0].origin == "web"
    finally:
        end_activity_request(token)


def test_filter_and_page_events_without_request_contents():
    factory = _session_factory()
    with factory() as db:
        record_http_activity(
            db, actor="admin", path="/finance/invoices/42", method="GET",
            route_path="/finance/invoices/{document_id}", status_code=200,
        )
        event = db.scalar(select(HubActivityEvent))
        assert event.module_key == "finance-invoices"
        assert event.resource_id == "42"
        assert event.action == "GET /finance/invoices/{document_id}"
        result = list_activity_events(db, {"protocol_module": "finance-invoices", "protocol_actor": "admin"})
        assert len(result["rows"]) == 1
        assert result["rows"][0][1] == "/finance/invoices/42"
        assert not result["has_next"]
        assert result["modules"]["finance-invoices"] == "Rechnungen"


def test_sensitive_changes_are_logged_without_values_and_rollbacks_are_discarded():
    factory = _session_factory()
    with factory() as db:
        customer = Customer(name="Beispiel")
        db.add(customer)
        db.commit()
        customer.encrypted_profile_json = "secret-customer-data"
        db.commit()
        events = db.scalars(select(HubActivityEvent).order_by(HubActivityEvent.id)).all()
        assert len(events) == 2
        assert events[1].category == "update"
        assert events[1].resource_id == str(customer.id)
        assert events[1].changed_fields == "Geschützte Daten"
        customer.name = "Nicht speichern"
        db.flush()
        db.rollback()
        assert len(db.scalars(select(HubActivityEvent)).all()) == 2


def test_path_classification():
    assert module_and_resource_for_path("/finance/recurring-invoices/163") == ("finance-recurring-invoices", "163")
    assert module_and_resource_for_path("/account/pdf-templates/5") == ("pdf-templates", "5")
    assert _module_for_object(HubFinanceGeneratedPdf(document_type="orders")) == "finance-orders"


def test_mailbox_polling_timestamps_are_not_recorded_as_activity():
    state = HubMailboxImapSyncState(last_synced_at=None, last_success_at=None, consecutive_failures=1)
    assert _changed_fields(state) == set()
    state.last_error = "connection failed"
    assert _changed_fields(state) == {"last_error"}
