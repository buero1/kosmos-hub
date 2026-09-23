import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import SecretCipher, build_request_signature, calculate_body_sha256
from app.db.base import Base
from app.models.customer import Customer
from app.models.site import Site
from app.services.customer_directory import CustomerDirectoryService
from app.services.site_customer_matching import SiteCustomerMatchingService, normalized_domain
from app.services.site_registration import SiteRegistrationService
from app.schemas.registration import RegistrationHeaders, RegistrationRequest


@pytest.fixture
def env():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        cipher = SecretCipher("d" * 32)
        yield db, cipher, SiteRegistrationService(db=db, cipher=cipher,
            settings=Settings(app_secret_key="x" * 32, database_url="sqlite://"))


def register(env, domain="alpe.example", identity=None, secret="s" * 32, endpoint=None, scheme="https"):
    identity = identity or str(uuid.uuid4())
    payload = RegistrationRequest(site_uuid=identity, site_secret=secret,
        home_url=f"{scheme}://{domain}/", site_url=f"{scheme}://{domain}/",
        wordpress_version="7.1.2", php_version="8.3", bridge_version="0.3.67",
        mcp_endpoint=endpoint or f"{scheme}://{domain}/wp-json/kosmos-bridge/v1/mcp",
        registration_timestamp=datetime.now(UTC))
    body = payload.model_dump_json().encode()
    stamp = datetime.now(UTC).isoformat()
    nonce = uuid.uuid4().hex
    sha = calculate_body_sha256(body)
    headers = RegistrationHeaders(site_uuid=identity, timestamp=stamp, nonce=nonce, body_sha256=sha,
        signature=build_request_signature(identity, stamp, nonce, sha, secret), request_id=nonce)
    return env[2].register(payload=payload, headers=headers, raw_body=body)


def customer(env, fields, **kwargs):
    db, cipher, _ = env
    item = Customer(name="Gasthof", is_visible=True, encrypted_profile_json=cipher.encrypt(json.dumps({
        "fields": fields,
    })), **kwargs)
    db.add(item)
    db.flush()
    return item


def test_domain_change_cannot_overwrite_identity_or_connection(env):
    db, _, _ = env
    result = register(env)
    original = db.get(Site, result.site_id)
    endpoint = original.connections[0].endpoint
    with pytest.raises(HTTPException) as exc:
        register(env, "copy.example", identity=result.site_uuid)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "bridge_domain_changed"
    db.rollback()
    assert original.domain == "alpe.example"
    assert original.connections[0].endpoint == endpoint
    assert len(db.scalars(select(Site)).all()) == 1
    copied = register(env, "copy.example", secret="n" * 32)
    assert copied.site_id != result.site_id
    assert original.connections[0].encrypted_credentials != db.get(Site, copied.site_id).connections[0].encrypted_credentials


def test_scheme_and_www_change_keeps_identity(env):
    result = register(env, scheme="http")
    second = register(env, "www.alpe.example", identity=result.site_uuid)
    assert second.site_id == result.site_id


def test_bad_signature_does_not_trigger_rotation_challenge(env):
    result = register(env)
    with pytest.raises(HTTPException) as exc:
        register(env, "copy.example", identity=result.site_uuid, secret="wrong" * 8)
    assert exc.value.status_code == 401


def test_cross_domain_endpoint_is_rejected(env):
    with pytest.raises(HTTPException) as exc:
        register(env, endpoint="https://other.example/wp-json/kosmos-bridge/v1/mcp")
    assert exc.value.status_code == 422
    env[0].rollback()
    assert not env[0].scalars(select(Site)).all()


@pytest.mark.parametrize("field", ["website", "Website", "Webseite", "Arbeitsdomain", "Arbeitsdomain-Login"])
def test_registration_matches_each_customer_domain_field(env, field):
    # Legacy metadata labels are resolved through the shared customer schema.
    c = customer(env, {field: "https://www.alpe.example/wp-admin"})
    if field == "Webseite":
        c.encrypted_profile_json = env[1].encrypt(json.dumps({"fields": {field: "alpe.example"},
            "field_metadata": {"website": {"label": "Webseite"}}}))
    env[0].commit()
    result = register(env)
    assert env[0].get(Site, result.site_id).customer_id == c.id


def test_ambiguous_customer_domains_never_auto_assign(env):
    customer(env, {"Website": "alpe.example"})
    customer(env, {"Arbeitsdomain-Login": "https://alpe.example/wp-admin"})
    env[0].commit()
    result = register(env)
    assert env[0].get(Site, result.site_id).customer_id is None


def test_later_customer_create_and_save_match_existing_site(env):
    result = register(env)
    db, cipher, _ = env
    service = CustomerDirectoryService(db=db, cipher=cipher)
    c = service.create_hub_customer(submitted_values={"customer_field__customer_name": "Alpe",
        "customer_field__account_status": "Neu", "customer_field__website": "other.example"})
    assert db.get(Site, result.site_id).customer_id is None
    service.update_hub_customer(customer_id=c.id, submitted_values={"customer_field__customer_name": "Alpe",
        "customer_field__account_status": "Neu", "customer_field__website": "https://alpe.example"})
    assert db.get(Site, result.site_id).customer_id == c.id
    db.rollback()
    assert db.get(Site, result.site_id).customer_id is None


def test_existing_assignment_is_never_replaced(env):
    c = customer(env, {"Website": "alpe.example"})
    env[0].commit()
    result = register(env)
    other = customer(env, {"Website": "alpe.example"})
    SiteCustomerMatchingService(db=env[0], cipher=env[1]).customer_saved(other)
    assert env[0].get(Site, result.site_id).customer_id == c.id


def test_old_domain_and_unreadable_profiles_do_not_guess_ownership(env):
    c = customer(env, {"Bisherige (alte) Website": "alpe.example"})
    env[0].commit()
    result = register(env)
    assert env[0].get(Site, result.site_id).customer_id is None
    c.encrypted_profile_json = "unreadable"
    customer(env, {"Website": "alpe.example"})
    env[0].commit()
    assert not SiteCustomerMatchingService(db=env[0], cipher=env[1]).link(env[0].get(Site, result.site_id))


@pytest.mark.parametrize(("value", "expected"), [
    ("https://WWW.Alpe.Example/wp-admin", "alpe.example"),
    ("http://alpe.example/", "alpe.example"), ("alpe.example.", "alpe.example"),
    ("https://alpe.example.evil.test", "alpe.example.evil.test"),
    ("https://alpe.example@evil.test", ""), ("javascript:alert(1)", ""),
    ("https://bad host.test", ""), ("https://alpe.example:bad", ""),
])
def test_domain_normalization(value, expected):
    assert normalized_domain(value) == expected
