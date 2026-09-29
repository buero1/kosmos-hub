"""Real local persistence and Elementor envelopes; never send a live mandate."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from app import main
from app.api.routes import sepa
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.db.record_info_tracking import install_record_info_tracking
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_record_info import HubRecordInfo
from app.models.hub_access_control import HubRecordAccessGrant
from app.models.hub_sepa_submission import HubSepaSubmission
from app.models.hub_user import HubUser
from app.models.zoho_email_template import ZohoEmailTemplate
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_sepa import HubSepaService, SepaError, TOKEN_FIELD, TOKEN_TTL
from app.services.template_placeholders import EMAIL_CUSTOMER_PLACEHOLDERS


NOW = datetime(2026, 9, 29, 22, 30, tzinfo=UTC)
IBAN = "DE89370400440532013000"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    install_record_info_tracking(factory)
    cipher = get_secret_cipher()
    with factory() as db:
        admin = HubUser(username="admin", password_hash="x", role="admin")
        viewer = HubUser(username="viewer", password_hash="x", role="viewer")
        sales = HubUser(username="sales", password_hash="x", role="sales")
        profile = {"fields": {"Kunde-Name": "Muster & Partner", "Kunde-Nummer": "20411",
                             "IBAN": "old", "BIC": "OLDVALUE", "Unbekannt": "preserve"},
                   "field_metadata": {}, "subforms": {"untouched": []}, "custom": "keep"}
        customer = Customer(name="Muster & Partner", zoho_id="old-zoho-id", is_visible=False,
                            encrypted_profile_json=cipher.encrypt(json.dumps(profile)))
        other = Customer(name="Other", encrypted_profile_json=cipher.encrypt(json.dumps(profile)))
        db.add_all([admin, viewer, sales, customer, other])
        HubAccessControlService(db=db).ensure_defaults()
        db.commit()
        monkeypatch.setattr(sepa, "SessionLocal", factory)
        sepa._rates.clear()
        yield SimpleNamespace(db=db, cipher=cipher, customer=customer, other=other, admin=admin,
                              service=HubSepaService(db=db, cipher=cipher), factory=factory, profile=profile)
    engine.dispose()


def issue(env, *, now=NOW, actor="admin"):
    return env.service.issue_link(customer=env.customer, actor=actor, now=now,
                                  context={"customername": "Muster & Partner", "customercustomernumber": "20411"})


def fields(token, **overrides):
    return {"token": token, "iban": IBAN, "bic": "COBADEFFXXX", "account_holder": "Erika Muster", **overrides}


def body(token, **overrides):
    return {TOKEN_FIELD: token, "ks_iban": IBAN, "ks_bic": "COBADEFFXXX",
            "field_a13f37a": "Erika Muster", **overrides}


def payload(env):
    env.db.refresh(env.customer)
    return json.loads(env.cipher.decrypt(env.customer.encrypted_profile_json))


def test_link_is_random_encodes_prefills_and_uses_customer_number(env):
    url, token = issue(env)
    query = parse_qs(urlsplit(url).query)
    assert query["firma"] == ["Muster & Partner"]
    assert query["ks_mandatsreferenz"] == ["20411"]
    assert "ks_account_id" not in query
    assert query[TOKEN_FIELD] == [token]
    assert token != issue(env)[1]
    claims, created, expires = env.service._claims(token, NOW)
    assert claims["customer"] == env.customer.id
    assert expires - created == timedelta(days=14)
    assert not env.db.new and not env.db.dirty  # Template GETs remain read-only.


def test_local_only_update_berlin_date_encryption_attribution_and_preservation(env):
    token = issue(env)[1]
    result = env.service.receive(fields(token, bank="Beispielbank"), now=NOW + timedelta(seconds=1))
    env.db.commit()
    saved = payload(env)
    assert result == {"success": True, "duplicate": False, "sepa_grant_date": "2026-09-30"}
    assert saved["fields"] == {**env.profile["fields"], "IBAN": IBAN, "BIC": "COBADEFFXXX",
                               "Kontoinhaber": "Erika Muster", "Bank": "Beispielbank",
                               "Datum SEPA-Erteilung": "2026-09-30"}
    assert saved["custom"] == "keep" and saved["subforms"] == env.profile["subforms"]
    assert saved["field_metadata"]["sepa_grant_date"]["display_type"] == "Datum"
    assert IBAN not in env.customer.encrypted_profile_json
    assert json.loads(env.cipher.decrypt(env.other.encrypted_profile_json)) == env.profile
    receipt = env.db.scalar(select(HubSepaSubmission))
    assert receipt.token_digest != token and IBAN not in receipt.payload_digest
    audit = env.db.scalar(select(AuditLog).where(AuditLog.action == "customer-sepa-received"))
    assert token not in audit.detail and IBAN not in audit.detail and "Erika" not in audit.detail
    info = env.db.scalar(select(HubRecordInfo).where(HubRecordInfo.record_table == "customers",
                                                  HubRecordInfo.record_id == env.customer.id))
    assert info.changed_name == "SEPA-Formular"


@pytest.mark.parametrize("offset,allowed", [(timedelta(days=14, microseconds=-1), True),
                                         (timedelta(days=14), False), (timedelta(seconds=-1), False)])
def test_exact_fourteen_day_window(env, offset, allowed):
    token = issue(env)[1]
    if allowed:
        assert env.service.receive(fields(token), now=NOW + offset)["success"]
    else:
        with pytest.raises(SepaError, match="ungueltig oder abgelaufen"):
            env.service.receive(fields(token), now=NOW + offset)
        assert payload(env) == env.profile


@pytest.mark.parametrize("when,expected", [("2026-01-01T23:30:00+00:00", "2026-01-02"),
                                          ("2026-03-29T22:15:00+00:00", "2026-03-30"),
                                          ("2026-10-25T23:15:00+00:00", "2026-10-26")])
def test_receipt_day_uses_berlin_not_form_or_utc(env, when, expected):
    instant = datetime.fromisoformat(when)
    token = issue(env, now=instant - timedelta(hours=1))[1]
    assert env.service.receive(fields(token, sepa_grant_date="1999-01-01"), now=instant)["sepa_grant_date"] == expected


def test_retries_are_idempotent_and_do_not_overwrite_later_manual_edits(env):
    token = issue(env)[1]
    env.service.receive(fields(token), now=NOW + timedelta(seconds=1))
    env.db.commit()
    edited = payload(env)
    edited["fields"]["Kontoinhaber"] = "Manual edit"
    env.customer.encrypted_profile_json = env.cipher.encrypt(json.dumps(edited))
    env.db.commit()
    assert env.service.receive(fields(token), now=NOW + timedelta(days=1))["duplicate"] is True
    env.db.commit()
    assert payload(env) == edited
    assert len(env.db.scalars(select(AuditLog)).all()) == 1
    with pytest.raises(SepaError, match="bereits verwendet"):
        env.service.receive(fields(token, account_holder="Another"), now=NOW + timedelta(days=1))


def test_other_older_link_cannot_overwrite_a_newer_submission(env):
    old_token = issue(env)[1]
    fresh_token = issue(env, now=NOW + timedelta(hours=1))[1]
    env.service.receive(fields(fresh_token), now=NOW + timedelta(hours=2))
    env.db.commit()
    with pytest.raises(SepaError, match="neuere"):
        env.service.receive(fields(old_token), now=NOW + timedelta(hours=3))


@pytest.mark.parametrize("actor", [None, "missing", "viewer", "sales"])
def test_issue_requires_actual_customer_edit_permission(env, actor):
    with pytest.raises(SepaError, match="Bearbeitungsberechtigung"):
        issue(env, actor=actor)


def test_disabled_issuer_and_revoked_link_cannot_change_customer(env):
    token = issue(env)[1]
    env.admin.is_active = False
    env.db.commit()
    with pytest.raises(SepaError):
        env.service.receive(fields(token), now=NOW + timedelta(seconds=1))
    env.admin.is_active = True
    env.db.commit()
    env.service.revoke(token, actor="admin", now=NOW)
    env.db.commit()
    with pytest.raises(SepaError):
        env.service.receive(fields(token), now=NOW + timedelta(seconds=1))
    assert payload(env) == env.profile


def test_record_read_grant_is_not_enough_and_revoked_edit_is_checked_at_receive(env):
    sales = env.db.scalar(select(HubUser).where(HubUser.username == "sales"))
    grant = HubRecordAccessGrant(module_key="customers", record_id=env.customer.id,
                                user_id=sales.id, can_edit=False)
    env.db.add(grant)
    env.db.commit()
    with pytest.raises(SepaError):
        issue(env, actor="sales")
    grant.can_edit = True
    env.db.commit()
    token = issue(env, actor="sales")[1]
    grant.can_edit = False
    env.db.commit()
    with pytest.raises(SepaError):
        env.service.receive(fields(token), now=NOW + timedelta(seconds=1))


def test_tampered_wrong_purpose_and_bare_customer_id_are_rejected(env):
    token = issue(env)[1]
    claims = json.loads(env.cipher.decrypt(token))
    claims["purpose"] = "some-other-link"
    for bad in (token[:-6] + "abcdef", str(env.customer.id), env.cipher.encrypt(json.dumps(claims))):
        with pytest.raises(SepaError):
            env.service.receive(fields(bad), now=NOW)
    assert payload(env) == env.profile


@pytest.mark.parametrize("overrides", [{"iban": "DE00370400440532013000"}, {"iban": ""},
                                     {"bic": "invalid!"}, {"account_holder": ""},
                                     {"account_holder": "<script>"}, {"account_holder": "a" * 256}])
def test_invalid_input_does_not_consume_token(env, overrides):
    token = issue(env)[1]
    with pytest.raises(SepaError):
        env.service.receive(fields(token, **overrides), now=NOW)
    env.db.rollback()
    assert env.db.scalar(select(HubSepaSubmission)) is None
    assert env.service.receive(fields(token), now=NOW + timedelta(seconds=1))["success"]


def test_empty_optional_fields_preserve_values_and_transaction_rolls_back(env):
    token = issue(env)[1]
    env.service.receive(fields(token, bic="", bank=""), now=NOW + timedelta(seconds=1))
    assert json.loads(env.cipher.decrypt(env.customer.encrypted_profile_json))["fields"]["BIC"] == "OLDVALUE"
    env.db.rollback()
    assert payload(env) == env.profile
    assert env.db.scalar(select(HubSepaSubmission)) is None
    assert env.service.receive(fields(token), now=NOW + timedelta(seconds=2))["success"]


@pytest.mark.parametrize("encoding", ["elementor-form", "elementor-json", "simple-json"])
def test_real_http_webhook_without_session_maps_current_elementor_ids(env, encoding):
    token = issue(env, now=datetime.now(UTC) - timedelta(seconds=2))[1]
    data = body(token, ks_account_id=str(env.other.id), ks_mandatsreferenz="WRONG", sepa_grant_date="1999-01-01")
    client = TestClient(main.create_app())  # No lifespan: never start background workers.
    if encoding == "elementor-form":
        kwargs = {"content": urlencode({f"fields[{key}][value]": value for key, value in data.items()}),
                  "headers": {"Content-Type": "application/x-www-form-urlencoded"}}
    elif encoding == "elementor-json":
        kwargs = {"json": {"form": {"id": "7898c25"}, "fields": {key: {"id": key, "value": value} for key, value in data.items()}}}
    else:
        kwargs = {"json": data}
    response = client.post(sepa.WEBHOOK_PATH, **kwargs)
    assert response.status_code == 200, response.text
    assert response.json()["success"] and response.headers["cache-control"] == "no-store"
    assert payload(env)["fields"]["IBAN"] == IBAN
    env.db.refresh(env.other)
    assert json.loads(env.cipher.decrypt(env.other.encrypted_profile_json)) == env.profile
    assert client.post(sepa.WEBHOOK_PATH, **kwargs).json()["duplicate"] is True
    assert client.get(sepa.WEBHOOK_PATH).status_code == 405


def test_http_bounds_missing_key_duplicates_and_auth_scope(env):
    client = TestClient(main.create_app())
    assert client.post(sepa.WEBHOOK_PATH, json={"ks_account_id": "1"}).status_code == 400
    assert client.post(sepa.WEBHOOK_PATH, json=body("fake")).status_code == 403
    assert client.post(sepa.WEBHOOK_PATH, content=b"x" * 32769).status_code == 413
    assert client.post(sepa.WEBHOOK_PATH, content=b"nope", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post(sepa.WEBHOOK_PATH, content='{"ks_iban":"a","ks_iban":"b"}',
                       headers={"Content-Type": "application/json"}).status_code == 400
    for pairs in ([('ks_hub_sepa_token', 'a'), ('ks_hub_sepa_token', 'b')],
                  [('field_a13f37a', 'A'), ('ks_kontoinhaber', 'B')]):
        assert client.post(sepa.WEBHOOK_PATH, content=urlencode(pairs),
                           headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert not main._is_public_hub_path("/api/v1/integrations/sepa/anything")
    assert not main._is_public_hub_path("/api/v1/integrations/callapp/closures")
    assert main._is_integration_api_path("/api/v1/integrations/callapp/closures")
    assert not main._is_integration_api_path(sepa.WEBHOOK_PATH)


def test_rate_limit(env):
    client = TestClient(main.create_app())
    for _ in range(120):
        response = client.post(sepa.WEBHOOK_PATH, json={})
        assert response.status_code == 400
    response = client.post(sepa.WEBHOOK_PATH, json={})
    assert response.status_code == 429 and response.headers["retry-after"] == "60"


@pytest.mark.parametrize(("override", "code"), [
    ({"ks_iban": ""}, "iban_missing"),
    ({"ks_iban": "not-an-iban"}, "iban_format"),
    ({"ks_iban": IBAN[:-1]}, "iban_length"),
    ({"ks_iban": "DE00370400440532013000"}, "iban_checksum"),
    ({"ks_bic": "invalid!"}, "bic_format"),
    ({"field_a13f37a": ""}, "account_holder_missing"),
    ({"field_a13f37a": "a" * 256}, "bank_text_length"),
    ({"field_a13f37a": "Private <Holder>"}, "bank_text_format"),
])
def test_rejection_diagnostics_exclude_bank_values_and_token(env, caplog, override, code):
    token = issue(env, now=datetime.now(UTC) - timedelta(seconds=2))[1]
    client = TestClient(main.create_app())
    data = body(token, **override)
    response = client.post(sepa.WEBHOOK_PATH, json={"fields": {
        key: {"value": value} for key, value in data.items()
    }})
    assert response.status_code == 422
    logs = [record.getMessage() for record in caplog.records if record.name == sepa.__name__]
    assert logs == [f"SEPA webhook rejected: status=422 code={code} fields=token,iban,bic,account_holder"]
    assert token not in caplog.text
    assert not any(value in caplog.text for value in data.values() if value)
    assert payload(env) == env.profile
    assert env.db.scalar(select(HubSepaSubmission)) is None


def test_missing_field_mapping_is_distinguishable_without_logging_unknown_names(env, caplog):
    token = issue(env, now=datetime.now(UTC) - timedelta(seconds=2))[1]
    client = TestClient(main.create_app())
    response = client.post(sepa.WEBHOOK_PATH, json={
        TOKEN_FIELD: token, "IBAN": IBAN, "Kontoinhaber": "Private Holder", "private-field-name": "private-value",
    })
    assert response.status_code == 422
    logs = [record.getMessage() for record in caplog.records if record.name == sepa.__name__]
    assert logs == ["SEPA webhook rejected: status=422 code=iban_missing fields=token"]
    for private in (token, IBAN, "Private Holder", "private-field-name", "private-value"):
        assert private not in caplog.text
    assert payload(env) == env.profile


def test_commit_failure_is_not_acknowledged_and_whole_transaction_rolls_back(env, monkeypatch):
    token = issue(env, now=datetime.now(UTC) - timedelta(seconds=2))[1]
    real_factory = sepa.SessionLocal
    def failing_factory():
        db = real_factory()
        def fail_commit():
            raise SQLAlchemyError("Do not log bank payload")
        db.commit = fail_commit
        return db
    monkeypatch.setattr(sepa, "SessionLocal", failing_factory)
    client = TestClient(main.create_app())
    response = client.post(sepa.WEBHOOK_PATH, json=body(token))
    assert response.status_code == 503 and response.json()["success"] is False
    assert response.headers["cache-control"] == "no-store"
    assert payload(env) == env.profile
    assert env.db.scalar(select(HubSepaSubmission)) is None
    monkeypatch.setattr(sepa, "SessionLocal", real_factory)
    assert client.post(sepa.WEBHOOK_PATH, json=body(token)).status_code == 200


def test_customer_template_link_placeholder_and_selected_contact(env):
    contact = CustomerContact(customer_id=env.customer.id, encrypted_profile_json=env.cipher.encrypt(json.dumps(
        {"fields": {"Name": "Erika Muster", "Vorname": "Erika", "Nachname": "Muster", "E-Mail": "erika@example.test"}})))
    template = ZohoEmailTemplate(zoho_template_id="sepa", module="Accounts", zoho_synced_at=NOW,
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"name": "SEPA", "subject": "Mandat",
            "content": '<p><a href="${Customer.SepaMandateUrl}">SEPA erteilen</a></p>', "hub_context_module": "customers"})))
    env.db.add_all([contact, template])
    env.db.commit()
    communications = CustomerCommunicationService(db=env.db, cipher=env.cipher, actor="admin", public_base_url="https://hub.test")
    result = communications.get_email_template(customer_id=env.customer.id, template_id="sepa",
                                               recipient_key=f"contact:{contact.id}:erika@example.test")
    assert not result.unresolved_placeholders
    assert "ks_hub_sepa_token=" in result.content and "ks_account_id" not in result.content
    assert "ks_mandatsreferenz=20411" in result.content
    assert "vorname=Erika" in result.content and "email=erika%40example.test" in result.content
    assert not env.db.new and not env.db.dirty
    assert "${Customer.SepaMandateUrl}" in {item.token for item in EMAIL_CUSTOMER_PLACEHOLDERS}
