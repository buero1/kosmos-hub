import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_customer_iban import IbanRevealError, reveal_customer_iban

IBAN = "DE89370400440532013000"


@pytest.fixture
def env(monkeypatch):
    from app.api.routes import web
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    cipher = SecretCipher("test-iban-reveal-key-32-characters-long")
    monkeypatch.setattr(web, "get_secret_cipher", lambda: cipher)
    with Session(engine) as db:
        user = HubUser(username="root", password_hash="x", role="admin", is_active=True, session_version=1)
        customer = Customer(name="Example", encrypted_profile_json=cipher.encrypt(json.dumps({
            "fields": {"IBAN": IBAN, "BIC": "COBADEFFXXX"},
            "field_metadata": {"iban": {"editable": True}},
        })))
        db.add_all([user, customer])
        db.commit()
        state = SimpleNamespace(user=user, version=1)
        app = FastAPI()
        app.add_api_route("/customers/{customer_id}/iban/reveal", web.reveal_customer_iban_value, methods=["POST"])
        app.dependency_overrides[web.get_db] = lambda: db

        @app.middleware("http")
        async def auth(request, call_next):
            request.state.hub_user = state.user
            request.scope["session"] = {"csrf_token": "csrf-test", "session_version": state.version}
            return await call_next(request)

        with TestClient(app) as client:
            yield SimpleNamespace(db=db, client=client, customer=customer, user=user, cipher=cipher, state=state)
    engine.dispose()


def post(env, **kwargs):
    return env.client.post(f"/customers/{env.customer.id}/iban/reveal", data={"csrf_token": "csrf-test", **kwargs})


def test_superadmin_reveal_is_audited_without_plaintext_persistence(env):
    encrypted = env.customer.encrypted_profile_json
    response = post(env)
    assert response.status_code == 200 and response.json() == {"iban": IBAN}
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    audit = env.db.scalar(select(AuditLog).where(AuditLog.action == "customer-iban-revealed"))
    assert audit and audit.actor == "root" and str(env.customer.id) in audit.detail
    assert IBAN not in audit.detail and IBAN not in encrypted
    assert env.customer.encrypted_profile_json == encrypted
    detail = CustomerDirectoryService(db=env.db, cipher=env.cipher).get_detail(customer_id=env.customer.id, include_sensitive=True)
    field = next(field for field in detail.profile_fields if field.key == "iban")
    assert field.value == "Geschützt" and field.form_value == ""


@pytest.mark.parametrize("role", ["management", "sales", "accounting", "viewer", "custom-admin", "superadmin"])
def test_other_roles_cannot_reveal_even_with_all_module_permissions(env, monkeypatch, role):
    from app.services.hub_access_control import HubAccessControlService
    env.user.role = role
    env.db.commit()
    monkeypatch.setattr(HubAccessControlService, "can", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(HubAccessControlService, "can_access_record", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(env.cipher, "decrypt", lambda *_args: pytest.fail("Unauthorized decrypt"))
    response = post(env)
    assert response.status_code == 403 and IBAN not in response.text
    assert "no-store" in response.headers["cache-control"]
    assert env.db.scalar(select(AuditLog)) is None
    with pytest.raises(IbanRevealError):
        reveal_customer_iban(db=env.db, cipher=env.cipher, user_id=env.user.id, session_version=1, customer_id=env.customer.id)


@pytest.mark.parametrize("kind", ["anonymous", "inactive", "revoked", "demoted", "csrf"])
def test_session_role_and_csrf_fail_closed(env, kind):
    if kind == "anonymous":
        env.state.user = None
    elif kind == "inactive":
        env.user.is_active = False
    elif kind == "revoked":
        env.user.session_version = 2
    elif kind == "demoted":
        env.state.user = SimpleNamespace(id=env.user.id, role="admin", is_active=True)
        env.user.role = "management"
    env.db.commit()
    response = post(env, csrf_token="wrong" if kind == "csrf" else "csrf-test")
    assert response.status_code == (401 if kind == "anonymous" else 403)
    assert IBAN not in response.text
    assert env.db.scalar(select(AuditLog)) is None


def test_no_get_reveal_or_unknown_customer(env):
    assert env.client.get(f"/customers/{env.customer.id}/iban/reveal").status_code == 405
    response = env.client.post("/customers/99999/iban/reveal", data={"csrf_token": "csrf-test"})
    assert response.status_code == 404 and IBAN not in response.text


@pytest.mark.parametrize("profile", [{"fields": {}}, {"fields": {"IBAN": ""}}, {"fields": {"IBAN": [IBAN]}}, []])
def test_empty_or_malformed_profile_never_leaks_other_data(env, profile):
    env.customer.encrypted_profile_json = env.cipher.encrypt(json.dumps(profile))
    env.db.commit()
    response = post(env)
    assert response.status_code in (404, 409) and "iban" not in response.json()
    assert env.db.scalar(select(AuditLog)) is None


def test_failed_audit_commit_does_not_disclose_value(env, monkeypatch, caplog):
    def fail():
        raise SQLAlchemyError("Sensitive error " + IBAN)
    monkeypatch.setattr(env.db, "commit", fail)
    response = post(env)
    assert response.status_code == 503 and IBAN not in response.text and IBAN not in caplog.text
    assert env.db.scalar(select(AuditLog)) is None


@pytest.mark.parametrize("role", ["admin", "management", "superadmin"])
def test_read_and_edit_controls_only_appear_for_superadmin_and_never_embed_iban(env, role):
    source = Path("app/templates/customer_detail.html").read_text(encoding="utf-8")
    names = ("customer_iban_reveal", "customer_profile_field_value", "customer_field_control")
    macros = "\n".join(re.search(r"{% macro " + name + r"\(.*?{% endmacro %}", source, re.S)[0] for name in names)
    template = Environment(autoescape=True).from_string(macros + "{{ customer_profile_field_value(field) }}{{ customer_field_control(field, 'customer_field__iban') }}")
    detail = CustomerDirectoryService(db=env.db, cipher=env.cipher).get_detail(customer_id=env.customer.id, include_sensitive=True)
    field = next(field for field in detail.profile_fields if field.key == "iban")
    html = template.render(field=field, detail=detail, csrf_token="csrf-test", request=SimpleNamespace(
        state=SimpleNamespace(hub_user=SimpleNamespace(role=role, is_active=True))))
    assert IBAN not in html and 'type="password"' in html and 'value=""' in html
    assert ('data-iban-reveal' in html) == (role == "admin")
