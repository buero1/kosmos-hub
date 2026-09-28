"""Regression coverage for the local-only Hub after retiring external CRM sync."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import main
from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerZohoEmail, CustomerZohoEmailImage
from app.models.zoho_connection import ZohoConnection
from app.models.zoho_email_history_import import ZohoEmailHistoryImport
from app.services.customer_directory import CustomerDirectoryService
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_profile_values import (
    normalize_date_value,
    date_control_value,
    patch_customer_profile,
)
from app.services.retire_external_crm import retire_external_crm
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS


@pytest.fixture
def local_db(monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("Unexpected external request"),
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db, SecretCipher("a" * 32)
    engine.dispose()


def profile():
    definitions = {field.key: field for field in ZOHO_ACCOUNT_FIELDS}
    keys = (
        "customer_name",
        "account_status",
        "website",
        "cancellation_date",
        "cancelled_at",
        "appointment_at",
        "iban",
    )
    return {
        "source": "zoho-crm",
        "extra": {"keep": True},
        "fields": {
            "Kunde-Name": "Example",
            "Status": "Aktuell",
            "Website": "https://example.test",
            "IBAN": "private",
            "Unbekannt": "keep",
        },
        "field_metadata": {
            key: {
                "label": definitions[key].label,
                "display_type": "Einzelzeile",
                "editable": True,
            }
            for key in keys
        },
        "subforms": {
            "history": {
                "metadata": {
                    "when": {
                        "label": "Datum",
                        "display_type": "Datum",
                        "editable": True,
                    }
                },
                "records": [
                    {
                        "id": "imported-row",
                        "values": {"when": "2026-01-01", "unknown": "keep"},
                    }
                ],
            }
        },
    }


def test_imported_customer_patch_is_local_and_preserves_history(local_db):
    db, cipher = local_db
    customer = Customer(
        name="Example",
        zoho_id="old-id",
        encrypted_profile_json=cipher.encrypt(json.dumps(profile())),
    )
    db.add(customer)
    db.flush()
    service = CustomerDirectoryService(db=db, cipher=cipher)
    service.update_hub_customer(
        customer_id=customer.id,
        submitted_values={
            "customer_field__cancellation_date": "31.12.2026",
            "customer_field__appointment_at": "2026-09-28T14:30",
            "customer_field__iban": "",
        },
    )
    stored = service._profile_data(customer)
    assert customer.zoho_id == "old-id"
    assert stored["fields"]["Kündigungsdatum"] == "2026-12-31"
    assert stored["fields"]["Terminzeit"] == "2026-09-28T14:30:00+02:00"
    assert stored["fields"]["IBAN"] == "private"
    assert stored["fields"]["Unbekannt"] == "keep"
    assert stored["subforms"] == profile()["subforms"]
    assert stored["extra"] == {"keep": True}
    before = customer.encrypted_profile_json
    with pytest.raises(ValueError, match="Datum"):
        service.update_hub_customer(
            customer_id=customer.id,
            submitted_values={"customer_field__cancellation_date": "2026-02-31"},
        )
    assert customer.encrypted_profile_json == before


def test_subform_patch_and_explicit_date_clear_preserve_others():
    original = profile()
    result = patch_customer_profile(
        original,
        {
            "customer_subform__history__0__when": "",
            "customer_subform__history__new__when": "2026-04-03",
        },
    )
    assert result["subforms"]["history"]["records"][0] == {
        "id": "imported-row",
        "values": {"when": None, "unknown": "keep"},
    }
    assert result["subforms"]["history"]["records"][1]["values"]["when"] == "2026-04-03"
    assert original == profile()


@pytest.mark.parametrize(
    "value,kind,expected",
    [
        ("31.12.2026", "Datum", "2026-12-31"),
        ("", "Datum", ""),
        ("2026-07-01T10:30:00Z", "DatumZeit", "2026-07-01T12:30:00"),
        ("2026-01-01T10:30:00+00:00", "DatumZeit", "2026-01-01T11:30:00"),
        ("2026-09-28T14:30", "DatumZeit", "2026-09-28T14:30:00"),
    ],
)
def test_date_controls_use_berlin_wall_time(value, kind, expected):
    assert date_control_value(value, kind) == expected


@pytest.mark.parametrize(
    "value,kind",
    [
        ("2026-02-30", "Datum"),
        ("garbage", "DatumZeit"),
        ("2026-03-29T02:30", "DatumZeit"),
        ("2026-01-01", "DatumZeit"),
    ],
)
def test_invalid_dates_are_rejected(value, kind):
    with pytest.raises(ValueError):
        normalize_date_value(value, kind)


def test_date_types_override_old_text_metadata_and_render_controls(local_db):
    from app.api.routes.web import templates
    from jinja2 import Environment

    db, cipher = local_db
    service = CustomerDirectoryService(db=db, cipher=cipher)
    fields = service._profile_fields_from_data(profile(), include_sensitive=True)
    values = {f.key: f for f in fields}
    assert values["cancelled_at"].display_type == "Datum"
    assert values["cancellation_date"].display_type == "Datum"
    assert values["appointment_at"].display_type == "DatumZeit"
    source = Path("app/templates/customer_detail.html").read_text(encoding="utf-8")
    start = source.index("{% macro customer_field_control(")
    end = source.index("{% endmacro %}", start) + len("{% endmacro %}")
    macro = (
        Environment(autoescape=True)
        .from_string(source[start:end])
        .module.customer_field_control
    )
    assert 'type="date"' in macro(values["cancellation_date"], "test")
    assert 'type="datetime-local"' in macro(values["appointment_at"], "test")
    for name in (
        "account.html",
        "customer_detail.html",
        "customer_contact_detail.html",
        "cases.html",
    ):
        templates.env.get_template(name)


def test_imported_contact_can_be_edited_and_linked_locally(local_db):
    db, cipher = local_db
    service = CustomerDirectoryService(db=db, cipher=cipher)
    contact = service.create_hub_contact(
        customer_id=None,
        submitted_values={
            "contact_field__salutation": "Herr",
            "contact_field__first_name": "Max",
            "contact_field__last_name": "Test",
            "contact_field__email": "max@example.test",
        },
    )
    contact.zoho_id = "legacy-contact"
    stored = service._profile_data(contact)
    stored["fields"]["Unknown"] = "keep"
    contact.encrypted_profile_json = cipher.encrypt(json.dumps(stored))
    service.update_hub_contact(
        contact_id=contact.id, submitted_values={"contact_field__phone": "123"}
    )
    customer = Customer(name="Local")
    db.add(customer)
    db.flush()
    service.set_hub_contact_customer(contact_id=contact.id, customer_id=customer.id)
    result = service._profile_data(contact)
    assert result["fields"]["Unknown"] == "keep"
    assert contact.zoho_id == "legacy-contact"
    assert contact.customer_id == customer.id


@pytest.mark.parametrize("legacy", [False, True])
def test_customer_notes_are_local_for_all_customers(local_db, legacy):
    db, cipher = local_db
    customer = Customer(name="Local", zoho_id="old-id" if legacy else None)
    db.add(customer)
    db.flush()
    service = CustomerCommunicationService(
        db=db, cipher=cipher, public_base_url="https://hub.example"
    )
    result = service.create_note(
        customer_id=customer.id, actor="user", title="Test", content="Original"
    )
    assert result.success
    service.update_note(
        customer_id=customer.id,
        note_id=result.note_id,
        title="Updated",
        content="Local change",
    )
    note = service._note_or_error(customer_id=customer.id, note_id=result.note_id)
    assert note.sync_status == "local"
    assert note.created_by_username == "user"
    assert service._note_view(note).content == "Local change"
    service.delete_note(customer_id=customer.id, note_id=note.id)


def test_retirement_purges_credentials_and_cancels_jobs_not_records(local_db):
    db, cipher = local_db
    connection = ZohoConnection(
        encrypted_client_id="encrypted",
        encrypted_client_secret="encrypted",
        encrypted_refresh_token="encrypted",
        scopes="all",
    )
    job = ZohoEmailHistoryImport(requested_by="admin", status="pending")
    customer = Customer(name="Keep", zoho_id="old-id")
    db.add_all([connection, job, customer])
    db.flush()
    retire_external_crm(db)
    retire_external_crm(db)
    db.expire_all()
    assert (
        connection.encrypted_refresh_token is None
        and connection.encrypted_client_secret == ""
    )
    assert job.status == "cancelled"
    assert db.get(Customer, customer.id).zoho_id == "old-id"


def test_no_external_crm_routes_workers_or_network_client_remain():
    paths = {route.path for route in main.app.routes if hasattr(route, "path")}
    assert not any("zoho" in path for path in paths)
    assert (
        not {
            "/cases/sync",
            "/contacts/sync",
            "/customers/{customer_id}/communications/sync",
        }
        & paths
    )
    from app.services import maintenance_worker

    assert not any("zoho" in name for name in vars(maintenance_worker))
    assert not Path("app/services/zoho_crm.py").exists()
    assert not Path("app/services/zoho_books.py").exists()
    assert not main._is_public_hub_path("/account/zoho/email-workflow-webhook/receive/old-token")
