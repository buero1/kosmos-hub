from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.core.record_actor import record_actor_scope
from app.core.security import SecretCipher
from app.core.templates import create_templates
from app.db.base import Base
from app.db.activity_tracking import install_activity_tracking
from app.db.record_info_tracking import install_record_info_tracking, RECORD_TABLES, PARENT_RECORDS
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoNote
from app.models.hub_activity_event import HubActivityEvent
from app.models.hub_finance_offer import HubFinanceOffer, HubFinanceOfferLine
from app.models.hub_record_info import HubRecordInfo
from app.models.hub_user import HubUser
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_lead_notes import HubLeadNoteService
from app.services.hub_accounts import HubAccountService
from app.services.hub_activity import begin_activity_request, end_activity_request, list_activity_events
from app.services.hub_operations import HubOperationService, get_operation
from app.services.hub_record_info import record_info, record_author
from app.services.hub_record_info_schema import ensure_record_info_schema


@pytest.fixture
def env():
    engine = create_engine("sqlite://")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    install_activity_tracking(factory)
    install_record_info_tracking(factory)
    with factory() as db:
        user = HubUser(username="s.mueller", first_name="Sarah", last_name="Müller", password_hash="hash", role="admin")
        db.add(user)
        db.commit()
        yield SimpleNamespace(db=db, user=user, engine=engine, factory=factory)
    engine.dispose()


def row(db, record):
    return db.execute(select(HubRecordInfo.__table__).where(HubRecordInfo.record_table == record.__tablename__,
                         HubRecordInfo.record_id == record.id)).mappings().one()


def test_names_are_independent_of_login_and_shared_with_agent(env):
    service = HubAccountService(db=env.db, app_secret_key="secret")
    created = service.create_user(username="employee", first_name="  Eva Maria  ", last_name=" Meier ",
        password="test-password-123", password_confirmation="test-password-123", role="viewer")
    assert created.display_name == "Eva Maria Meier"
    service.update_user(user_id=created.id, username="employee.new", role="viewer", last_name="Schmidt")
    assert created.first_name == "Eva Maria" and created.display_name == "Eva Maria Schmidt"
    assert "first_name" in {field.name for field in get_operation("access.users.update").input_fields()}
    gateway = HubOperationService(db=env.db, cipher=SecretCipher("secret"), actor=env.user.username)
    gateway.execute("access.users.update", {"user_id": str(created.id), "first_name": "Eva", "last_name": "Schulz"})
    assert created.display_name == "Eva Schulz"
    with pytest.raises(ValueError):
        service.update_user(user_id=created.id, username="employee.new", role="viewer", first_name="a" * 101)
    assert created.first_name == "Eva"


def test_http_creator_snapshot_survives_login_and_name_change(env):
    token = begin_activity_request(env.user.username, "/customers", "POST", user=env.user)
    try:
        customer = Customer(name="Example")
        env.db.add(customer)
        env.db.commit()
    finally:
        end_activity_request(token)
    original = row(env.db, customer)
    assert original["created_user_id"] == env.user.id
    assert record_info(customer)["created_by"] == "Sarah Müller"
    env.user.username, env.user.last_name = "s.schmidt", "Schmidt"
    env.db.commit()
    with record_actor_scope(env.db, env.user.username):
        customer.name = "Changed"
        env.db.commit()
    updated = row(env.db, customer)
    assert updated["created_at"] == original["created_at"]
    assert updated["created_name"] == "Sarah Müller" and updated["changed_name"] == "Sarah Schmidt"
    assert updated["changed_user_id"] == original["created_user_id"]
    events = list_activity_events(env.db, {"protocol_actor": "s.schmidt", "protocol_module": "customers"})["rows"]
    assert len(events) == 2
    assert events[1][0].actor_name == "Sarah Müller"


def test_agent_line_only_edit_updates_parent_but_rollback_does_not(env):
    offer = HubFinanceOffer(encrypted_fields_json="{}", lines=[HubFinanceOfferLine(position_index=0, encrypted_fields_json="old")])
    with record_actor_scope(env.db, env.user.username):
        env.db.add(offer)
        env.db.commit()
    before = row(env.db, offer)
    with record_actor_scope(env.db, env.user.username, origin="agent"):
        offer.lines[0].encrypted_fields_json = "new"
        env.db.commit()
    assert record_info(offer)["changed_by"] == "Sarah Müller · über Hub-Agent"
    after = row(env.db, offer)
    assert after["created_at"] == before["created_at"]
    with record_actor_scope(env.db, env.user.username):
        offer.lines[0].encrypted_fields_json = "aborted"
        env.db.flush()
        env.db.rollback()
    assert row(env.db, offer) == after
    assert offer.lines[0].encrypted_fields_json == "new"


def test_reads_routine_updates_noops_and_notes_do_not_change_customer(env):
    customer = Customer(name="Example")
    env.db.add(customer)
    env.db.commit()
    original = row(env.db, customer)
    customer.name = "Example"
    customer.zoho_synced_at = datetime.now(UTC)
    env.db.commit()
    assert row(env.db, customer) == original
    token = begin_activity_request(env.user.username, "/customers/1", "GET", user=env.user)
    try:
        customer.updated_at = datetime.now(UTC)
        env.db.commit()
    finally:
        end_activity_request(token)
    assert row(env.db, customer) == original
    with record_actor_scope(env.db, env.user.username):
        note = CustomerZohoNote(customer_id=customer.id, source="hub", sync_status="synced", encrypted_payload_json="{}")
        env.db.add(note)
        env.db.commit()
    assert record_author(note) == "Sarah Müller"
    assert row(env.db, customer) == original


def test_deleting_last_line_counts_as_a_document_change(env):
    offer = HubFinanceOffer(encrypted_fields_json="{}", lines=[HubFinanceOfferLine(position_index=0, encrypted_fields_json="old")])
    env.db.add(offer)
    env.db.commit()
    with record_actor_scope(env.db, env.user.username, origin="agent"):
        offer.lines.clear()
        env.db.commit()
    assert record_info(offer)["changed_by"] == "Sarah Müller · über Hub-Agent"


@pytest.mark.parametrize("module", ["customers", "leads"])
@pytest.mark.parametrize("origin", ["ui", "agent", "historical"])
def test_note_views_keep_employee_or_historical_author_and_timestamp(env, module, origin):
    cipher = SecretCipher("note-test")
    when = datetime(2026, 7, 28, 9, 30, 41)
    values = dict(encrypted_payload_json=cipher.encrypt('{"Note_Content":"Example"}'),
                  created_by_username="Team Kosmos" if origin == "historical" else env.user.username,
                  zoho_created_at=when)
    if module == "customers":
        parent = Customer(name="Example")
        note = CustomerZohoNote(customer=parent, source="zoho" if origin == "historical" else "hub",
                               zoho_note_id="external" if origin == "historical" else None, **values)
        view = CustomerCommunicationService(db=env.db, cipher=cipher, public_base_url="")._note_view
    else:
        parent = HubLead(encrypted_profile_json=cipher.encrypt("{}"))
        note = HubLeadNote(lead=parent, zoho_note_id="external" if origin == "historical" else "hub-example",
                           zoho_imported_at=when, **values)
        view = HubLeadNoteService(db=env.db, cipher=cipher)._view
    with record_actor_scope(env.db, env.user.username, origin="agent" if origin == "agent" else "ui"):
        env.db.add(note)
        env.db.commit()
    displayed = view(note)
    expected = "Team Kosmos" if origin == "historical" else env.user.display_name
    if origin == "agent":
        expected += " · über Hub-Agent"
    assert displayed.author == expected
    assert displayed.occurred_at == when


def test_import_dates_and_unknown_people_are_not_fabricated(env, monkeypatch):
    cipher = SecretCipher("source-test")
    monkeypatch.setattr("app.core.security.get_secret_cipher", lambda: cipher)
    customer = Customer(name="Imported", zoho_id="external", created_at=datetime(2026, 9, 1, tzinfo=UTC),
        zoho_modified_at=datetime(2025, 3, 2, tzinfo=UTC))
    env.db.add(customer)
    env.db.commit()
    data = record_info(customer)
    assert data["created_label"] == "Im Hub seit"
    assert data["created_by"] == data["changed_by"] == "Nicht bekannt"
    assert data["changed_at"].year == 2025
    # Historical fields may be retained encrypted, even after connector labels disappear.
    customer.encrypted_profile_json = cipher.encrypt('{"Created_Time": "2024-03-01T12:00:00Z"}')
    env.db.flush()
    assert record_info(customer)["created_at"].year == 2024
    assert record_info(customer)["created_label"] == "Erstellt am"
    assert record_info(customer)["created_by"] == "Nicht bekannt"


def test_user_deletion_keeps_name_and_record_deletion_removes_metadata(env):
    with record_actor_scope(env.db, env.user.username):
        customer = Customer(name="Example")
        env.db.add(customer)
        env.db.commit()
    env.db.delete(env.user)
    env.db.commit()
    assert row(env.db, customer)["created_user_id"] is None
    assert record_info(customer)["created_by"] == "Sarah Müller"
    identifier = customer.id
    env.db.delete(customer)
    env.db.commit()
    assert env.db.scalar(select(HubRecordInfo).where(HubRecordInfo.record_table == "customers", HubRecordInfo.record_id == identifier)) is None


def test_schema_upgrade_is_additive_and_restart_safe():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE hub_users (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
        connection.execute(text("CREATE TABLE hub_activity_events (id INTEGER PRIMARY KEY, actor VARCHAR(64))"))
        connection.execute(text("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, actor VARCHAR(64))"))
        connection.execute(text("INSERT INTO hub_users VALUES (1, 'legacy')"))
    ensure_record_info_schema(engine)
    ensure_record_info_schema(engine)
    assert {"first_name", "last_name"} <= {column["name"] for column in inspect(engine).get_columns("hub_users")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT username, first_name FROM hub_users")).one() == ("legacy", None)


def test_shared_panel_renders_escaped_names_and_has_no_connector_labels(env):
    customer = Customer(name="Example")
    env.db.add(customer)
    env.db.commit()
    env.user.first_name = "<script>"
    with record_actor_scope(env.db, env.user.username):
        customer.name = "Changed"
        env.db.commit()
    templates = create_templates(directory="app/templates")
    html = templates.env.from_string('{% from "partials/hub_data_panel.html" import hub_data_panel with context %}{{ hub_data_panel(record) }}').render(record=customer)
    assert "Erstellt von" in html and "Geändert von" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "Verknüpfungen" not in html and "Zoho" not in html


def test_details_share_panel_and_tracked_models_instead_of_connector_metadata():
    models = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    assert RECORD_TABLES <= models.keys()
    for table, (_, foreign_key) in PARENT_RECORDS.items():
        assert hasattr(models[table], foreign_key)
    for name in ("customer", "customer_contact", "lead", "case", "finance_offer", "finance_document", "finance_article", "site"):
        source = Path(f"app/templates/{name}_detail.html").read_text(encoding="utf-8")
        assert 'partials/hub_data_panel.html' in source
        for text in ("Letzte Änderung in Zoho", "Last changed in Zoho", "Zoho Books-ID", "Zoho Lead-ID", "Zoho account ID", "<dt>Importiert am</dt>"):
            assert text not in source
