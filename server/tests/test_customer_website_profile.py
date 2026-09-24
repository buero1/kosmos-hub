import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.models.hub_wordpress_job import HubWordPressJob
from app.models.site import Site
from app.models.site_connection import SiteConnection
from app.services.customer_website_profile import preview, prepare, TEXT_IDS, READ_ABILITY, WRITE_ABILITY
from app.services.hub_operations import HubOperationService, HubOperationError, get_operation
from app.services.site_mcp_proxy import SiteMcpProxyService, SiteMcpProxyError, NoBridgeRedirect
from app.services.wordpress_jobs import process_next


FIELDS = {"company_name": {"label": "Firma", "type": "text", "max_length": 500},
          "phone": {"label": "Telefon", "type": "text", "max_length": 500},
          "email": {"label": "Email", "type": "email", "max_length": 254},
          "custom": {"label": "Freies Feld", "type": "text", "max_length": 500},
          "logo": {"label": "Logo", "type": "image", "max_length": 500}}
PROFILE = {"customer_name": "Example Customer", "website": "https://main.example",
           "work_domain": "https://work.example", "work_domain_login": "https://login.example/wp-admin",
           "phone": "+49 123 456", "billing_street": "Example Street 1",
           "billing_postal_code": "12345", "billing_city": "Example City"}


@pytest.fixture
def context(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    cipher = SecretCipher("profile-transfer-tests")
    calls = []
    remote = {"schema": {"fields": deepcopy(FIELDS)}, "values": {"company_name": "Old", "email": "keep@example.test", "custom": "Keep", "logo": 2},
              "revision": "a" * 64}
    def execute(_self, site_id, ability, data, **kwargs):
        assert kwargs["strict_transport"] is True
        calls.append((site_id, ability, data))
        if ability == READ_ABILITY:
            return {"result": deepcopy(remote)}
        assert ability == WRITE_ABILITY
        if remote.get("error"):
            raise remote["error"]
        if data["revision"] != remote["revision"]:
            raise SiteMcpProxyError("CONFLICT", "Stale", status_code=409)
        remote["values"].update(data["values"])
        remote["revision"] = "b" * 64
        return {"result": {"values": data["values"], "revision": remote["revision"]}}
    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute)
    with sessions() as db:
        db.add_all([HubUser(username="admin", role="admin", password_hash="hash"),
                    HubUser(username="other", role="admin", password_hash="hash"),
                    HubUser(username="viewer", role="viewer", password_hash="hash"),
                    Customer(id=1, name="Example Customer", encrypted_profile_json=cipher.encrypt(json.dumps({"fields": PROFILE})))])
        db.flush()
        for number, domain in enumerate(("main.example", "work.example", "login.example"), 1):
            site = Site(id=number, customer_id=1, uuid=f"uuid-{number}", domain=domain,
                        home_url=f"https://{domain}", site_url=f"https://{domain}", status="verified")
            site.connections.append(SiteConnection(provider="kosmos-wordpress", endpoint=f"https://{domain}/wp-json/kosmos-bridge/v1/mcp",
                                                    auth_type="hmac", encrypted_credentials=cipher.encrypt("test")))
            db.add(site)
        db.commit()
    yield sessions, cipher, calls, remote
    engine.dispose()


def service(db, cipher, actor="admin"):
    return HubOperationService(db=db, cipher=cipher, actor=actor)


def inputs(data, fields=("company_name",)):
    return {"customer_id": data["customer_id"], "site_id": data["site_id"],
            "preview_token": data["preview_token"], "field_ids": json.dumps(fields)}


@pytest.mark.parametrize("remove, expected, source", [
    ((), 1, "Website URL"), (("website",), 2, "Arbeitsdomain URL"),
    (("website", "work_domain"), 3, "Arbeitsdomain-Login URL")])
def test_exact_priority_and_login_root(context, remove, expected, source):
    sessions, cipher, calls, _ = context
    with sessions() as db:
        fields = {**PROFILE, **{key: "" for key in remove}}
        db.get(Customer, 1).encrypted_profile_json = cipher.encrypt(json.dumps({"fields": fields}))
        data = service(db, cipher).query("customers.website_profile.preview", {"customer_id": "1"})
        assert data["site_id"] == str(expected)
        assert data["target_source"] == source
        assert calls[0][:2] == (expected, READ_ABILITY)


@pytest.mark.parametrize("fault", ["missing", "inactive", "unlinked", "insecure", "foreign_endpoint", "invalid", "empty"])
def test_no_silent_fallback_or_write_on_bad_target(context, fault):
    sessions, cipher, calls, _ = context
    with sessions() as db:
        site = db.get(Site, 1)
        if fault == "missing":
            site.status = "offline"
        elif fault == "inactive":
            site.connections[0].status = "error"
        elif fault == "unlinked":
            site.customer_id = None
        elif fault == "insecure":
            site.connections[0].endpoint = "http://main.example/mcp"
        elif fault == "foreign_endpoint":
            site.connections[0].endpoint = "https://other.example/mcp"
        else:
            fields = {**PROFILE, "website": "javascript:invalid"} if fault == "invalid" else {}
            db.get(Customer, 1).encrypted_profile_json = cipher.encrypt(json.dumps({"fields": fields}))
        with pytest.raises(HubOperationError):
            preview(service(db, cipher), 1)
        assert not calls


def test_alternative_must_be_own_linked_candidate(context):
    sessions, cipher, _, _ = context
    with sessions() as db:
        assert preview(service(db, cipher), 1, 2)["site_id"] == "2"
        db.get(Site, 3).customer_id = None
        with pytest.raises(HubOperationError):
            preview(service(db, cipher), 1, 3)


def test_stable_ids_empty_unknown_and_image_values_are_preserved(context):
    sessions, cipher, _, remote = context
    remote["schema"]["fields"].update({key: {"label": "Renamed by owner", "type": "text", "max_length": 1000} for key in TEXT_IDS})
    with sessions() as db:
        data = preview(service(db, cipher), 1)
        rows = {row["id"]: row for row in data["rows"]}
        assert rows["company_name"]["proposed"] == "Example Customer"
        for key in ("email", "custom", "logo"):
            assert not rows[key]["selectable"] and not rows[key]["proposed"]
        address_id = next(key for key, value in TEXT_IDS.items() if value == "address")
        assert rows[address_id]["proposed"] == "Example Street 1, 12345 Example City"
        assert "Example Customer" not in data["preview_token"]
        for key in ("email", "custom", "logo"):
            with pytest.raises(HubOperationError):
                prepare(service(db, cipher), customer_id=1, site_id=1, preview_token=data["preview_token"], field_ids=[key])


@pytest.mark.parametrize("fault", ["forged", "expired", "actor", "customer_changed", "uuid_changed", "revoked", "duplicates", "no_fields"])
def test_confirmation_is_bound_to_actor_customer_target_time_and_fields(context, fault):
    sessions, cipher, calls, _ = context
    with sessions() as db:
        data = preview(service(db, cipher), 1)
        token = data["preview_token"]
        actor, fields = "admin", ["company_name"]
        if fault == "forged":
            token = token[:-4] + "fake"
        elif fault == "expired":
            proof = json.loads(cipher.decrypt(token))
            proof["expires"] = datetime.now(UTC).timestamp() - 1
            token = cipher.encrypt(json.dumps(proof))
        elif fault == "actor":
            actor = "other"
        elif fault == "customer_changed":
            db.get(Customer, 1).name = "Changed"
        elif fault == "uuid_changed":
            db.get(Site, 1).uuid = "different"
        elif fault == "revoked":
            db.scalar(select(HubUser).where(HubUser.username == "admin")).is_active = False
        elif fault == "duplicates":
            fields *= 2
        else:
            fields = []
        with pytest.raises(HubOperationError):
            prepare(service(db, cipher, actor), customer_id=1, site_id=1, preview_token=token, field_ids=fields)
        assert len(calls) == 1


def test_unauthorized_read_never_contacts_website(context):
    sessions, cipher, calls, _ = context
    with sessions() as db:
        with pytest.raises(HubOperationError):
            preview(service(db, cipher, "viewer"), 1)
        assert calls == []


@pytest.mark.parametrize("outcome", ["succeeded", "conflict", "uncertain", "revoked", "source_changed"])
def test_queued_execution_and_failures_never_retry_or_leak_values(context, outcome):
    sessions, cipher, calls, remote = context
    with sessions() as db:
        gateway = service(db, cipher)
        data = gateway.query("customers.website_profile.preview", {"customer_id": "1"})
        args = inputs(data)
        result = gateway.execute("wordpress.company_profile.send", args)
        job_id = result.record_id
        job = db.get(HubWordPressJob, job_id)
        assert len(calls) == 1
        assert data["preview_token"] not in job.encrypted_input
        assert data["preview_token"] not in str(get_operation("wordpress.company_profile.send").preview(args))
        db.commit()
        if outcome == "conflict":
            remote["revision"] = "c" * 64
        elif outcome == "uncertain":
            remote["error"] = OSError("private transport error")
        elif outcome == "revoked":
            db.scalar(select(HubUser).where(HubUser.username == "admin")).is_active = False
            db.commit()
        elif outcome == "source_changed":
            db.get(Customer, 1).name = "New name"
            db.commit()
    assert process_next(sessions, cipher)
    assert not process_next(sessions, cipher)
    with sessions() as db:
        job = db.get(HubWordPressJob, job_id)
        expected = "succeeded" if outcome == "succeeded" else "uncertain" if outcome == "uncertain" else "failed"
        assert job.status == expected
        assert job.encrypted_input is None
        assert "Example Customer" not in str(job.result_json)
        assert "private transport" not in str(job.message)
    assert len(calls) == (1 if outcome in {"revoked", "source_changed"} else 2)
    assert remote["values"]["email"] == "keep@example.test"
    assert remote["values"]["logo"] == 2
    assert remote["values"]["company_name"] == ("Example Customer" if outcome == "succeeded" else "Old")


def test_signed_requests_never_follow_redirects():
    assert NoBridgeRedirect().redirect_request(None, None, 307, "", {}, "https://other.example") is None


def test_http_routes_share_catalog_require_csrf_and_confirmation(context, monkeypatch):
    from app.api.routes import web
    from fastapi import HTTPException
    from starlette.requests import Request
    sessions, cipher, calls, _ = context
    with sessions() as db:
        request = Request({"type": "http", "method": "POST", "path": "/customers/1/website-profile/send", "headers": []})
        request.state.hub_user = db.scalar(select(HubUser).where(HubUser.username == "admin"))
        monkeypatch.setattr(web, "get_secret_cipher", lambda: cipher)
        response = web.customer_website_profile_preview(1, request, db)
        assert response.headers["cache-control"] == "private, no-store"
        data = json.loads(response.body)
        def reject(*args):
            raise HTTPException(403, "csrf")
        monkeypatch.setattr(web, "require_csrf", reject)
        with pytest.raises(HTTPException, match="csrf"):
            web.customer_website_profile_send(1, request, db, 1, data["preview_token"], ["company_name"], "yes", "bad")
        monkeypatch.setattr(web, "require_csrf", lambda *_: None)
        with pytest.raises(HTTPException):
            web.customer_website_profile_send(1, request, db, 1, data["preview_token"], ["company_name"], "", "token")
        assert not db.scalars(select(HubWordPressJob)).all()
        response = web.customer_website_profile_send(1, request, db, 1, data["preview_token"], ["company_name"], "yes", "token")
        assert json.loads(response.body)["status"] == "queued"
        assert len(calls) == 1


def test_partial_template_escapes_customer_and_uses_shared_drawer():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    html = env.get_template("partials/customer_website_profile.html").render(
        detail={"entry": {"customer": {"id": 1, "name": "<img src=x onerror=alert(1)>"}}}, csrf_token="safe")
    assert "&lt;img" in html and "<img" not in html
    assert 'role="dialog"' in html and 'data-customer-id="1"' in html
