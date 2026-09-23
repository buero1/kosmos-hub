from types import SimpleNamespace

from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.db.activity_tracking import _changed_fields, _module_for_object, install_activity_tracking
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_activity_event import HubActivityEvent
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.services.audit import write_audit_log
from app.services.hub_activity import (
    activity_resource_url,
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
        assert result["rows"][0][1] == "/finance/invoices/42?from_protocol=1"
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


@pytest.mark.parametrize("path,method,status,expected", [
    ("/account", "GET", 200, False),
    ("/account/", "GET", 200, False),
    ("/account", "HEAD", 200, False),
    ("/account", "GET", 304, False),
    ("/account", "GET", 403, True),
    ("/account", "GET", 500, True),
    ("/account", "POST", 200, True),
    ("/account/password", "POST", 200, True),
    ("/account/users/3", "DELETE", 200, True),
    ("/account/pdf-templates/4/download", "GET", 200, True),
    ("/customers/42", "GET", 200, True),
    ("/leads/5", "GET", 200, True),
    ("/settings", "GET", 200, True),
    ("/static/app.js", "GET", 200, False),
])
def test_account_protocol_exclusion_is_limited_to_successful_overview_reads(path, method, status, expected):
    from app.main import _should_record_http_activity

    request = Request({
        "type": "http", "path": path, "method": method,
        "headers": [(b"accept", b"text/html")],
        "query_string": b"protocol_page=2&protocol_module=account",
    })
    assert _should_record_http_activity(request, status_code=status) is expected


def test_protocol_navigation_does_not_log_itself_but_other_actions_remain_logged(monkeypatch):
    from app import main

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    install_activity_tracking(factory)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "_authenticated_hub_user", lambda request: SimpleNamespace(username="reader", role="admin"))
    app = main.create_app()
    # Exercise the real middleware against local-only routes, without production
    # startup workers, integrations or application data.
    app.router.routes.clear()

    @app.api_route("/account", methods=["GET", "HEAD", "POST"])
    def account(request: Request):
        return HTMLResponse("Protocol", status_code=500 if request.query_params.get("error") else 200)

    @app.get("/customers/{customer_id}")
    def customer(customer_id: int):
        return HTMLResponse("Customer")

    @app.post("/account/password")
    def change_password():
        with factory() as db:
            write_audit_log(db, site=None, actor="reader", source="hub-account", action="change-password", result="ok", detail=None)
            db.commit()
        return HTMLResponse("Saved")

    client = TestClient(app)
    for url in (
        "/account#account-protocol", "/account?protocol_module=account#account-protocol",
        "/account?protocol_page=2#account-protocol", "/account?protocol_page=1#account-protocol",
        "/account", "/account/",
    ):
        assert client.get(url, headers={"Accept": "text/html"}).status_code == 200
    assert client.head("/account", headers={"Accept": "text/html"}).status_code == 200
    protocol_link = activity_resource_url(HubActivityEvent(module_key="customers", resource_id="42"))
    assert client.get(protocol_link, headers={"Accept": "text/html"}).status_code == 200
    with factory() as db:
        assert db.scalars(select(HubActivityEvent)).all() == []

    assert client.get("/customers/42", headers={"Accept": "text/html"}).status_code == 200
    assert client.post("/account/password?from_protocol=1").status_code == 200
    assert client.post("/account").status_code == 200
    assert client.get("/account?error=1", headers={"Accept": "text/html"}).status_code == 500
    with factory() as db:
        events = db.scalars(select(HubActivityEvent).order_by(HubActivityEvent.id)).all()
        assert [(event.module_key, event.category, event.result) for event in events] == [
            ("customers", "view", "success"), ("account", "update", "ok"),
            ("account", "execute", "success"), ("account", "view", "error"),
        ]
        assert events[0].resource_id == "42"
        assert events[1].action == "change-password"
    client.close()


@pytest.mark.parametrize("path,method,status,role,expected", [
    ("/customers/42", "GET", 200, "admin", False),
    ("/leads/5", "GET", 200, "admin", False),
    ("/finance/offers/8", "GET", 200, "admin", False),
    ("/customers", "GET", 200, "admin", False),
    ("/settings", "GET", 200, "admin", False),
    ("/customers/42", "HEAD", 200, "admin", False),
    ("/customers/42", "POST", 200, "admin", True),
    ("/customers/42", "DELETE", 200, "admin", True),
    ("/customers/42", "GET", 404, "admin", True),
    ("/customers/42", "GET", 200, "member", True),
    ("/finance/offers/8/pdf", "GET", 200, "admin", True),
    ("/account/pdf-templates/4/download", "GET", 200, "admin", True),
    ("/customers/42/communications/emails/3/attachments/4", "GET", 200, "admin", True),
    ("/account/logout", "GET", 200, "admin", True),
])
def test_protocol_link_marker_never_suppresses_writes_errors_or_downloads(path, method, status, role, expected):
    from app.main import _should_record_http_activity

    request = Request({
        "type": "http", "path": path, "method": method,
        "headers": [(b"accept", b"text/html")], "query_string": b"from_protocol=1",
        "state": {"hub_user": SimpleNamespace(role=role)},
    })
    assert _should_record_http_activity(request, status_code=status) is expected


@pytest.mark.parametrize("module,record_id,url", [
    ("customers", "42", "/customers/42?from_protocol=1"),
    ("leads", None, "/leads?from_protocol=1"),
    ("account", None, "/account?from_protocol=1"),
    ("pdf-templates", "5", "/account?from_protocol=1#account-pdf-templates"),
    ("users", "3", "/account?from_protocol=1#account-users"),
    ("unknown", None, None),
])
def test_protocol_links_keep_their_target_and_section(module, record_id, url):
    assert activity_resource_url(HubActivityEvent(module_key=module, resource_id=record_id)) == url
