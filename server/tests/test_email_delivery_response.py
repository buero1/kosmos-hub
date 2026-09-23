from types import SimpleNamespace
import shutil
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.customer_communications import CustomerCommunicationActionResult
from app.services.hub_mailbox import HubMailboxService


@pytest.fixture
def delivery_client(monkeypatch):
    from app.api.routes import web

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    monkeypatch.setattr(web, "get_secret_cipher", lambda: cipher)
    with Session(engine) as db:
        user = HubUser(username="email-author", password_hash="x", role="admin")
        customer = Customer(name="Example")
        db.add_all([user, customer])
        db.flush()
        draft = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test").save_draft(
            draft_id=None, sender_email="team@example.test", recipient_email="recipient@example.test",
            recipient_key="", recipient_customer_id=None, recipient_name="", subject="Unchanged draft",
            content="<p>Original content</p>", cc_emails="", template_id="", reply_to_email_id="", forward_from_email_id="",
        )
        db.commit()
        app = FastAPI()
        app.add_api_route("/emails/send", web.send_direct_mailbox_email, methods=["POST"])
        app.add_api_route("/customers/{customer_id}/communications/emails", web.send_customer_communication_email, methods=["POST"])
        app.dependency_overrides[web.get_db] = lambda: db

        @app.middleware("http")
        async def signed_in_request(request, call_next):
            request.state.hub_user = user
            request.scope["session"] = {"csrf_token": "test-csrf"}
            return await call_next(request)

        with TestClient(app) as client:
            yield SimpleNamespace(client=client, db=db, draft_id=draft.id, customer_id=customer.id, cipher=cipher)


@pytest.mark.parametrize("kind", ["direct", "customer-exception", "customer-result", "scheduled"])
def test_failed_delivery_returns_only_error_and_preserves_draft(delivery_client, monkeypatch, kind):
    from app.api.routes import web

    fixture = delivery_client
    message = "Empfaengeradresse vom Mailserver abgelehnt. Bitte pruefen."
    calls = []

    def fail_send(*_args, **kwargs):
        calls.append(kwargs)
        if kind == "customer-result":
            return CustomerCommunicationActionResult(False, message)
        raise ValueError(message)

    monkeypatch.setattr(web.HubMailboxService, "send_direct_email", fail_send)
    monkeypatch.setattr(web.ScheduledEmailService, "schedule", fail_send)
    monkeypatch.setattr(web, "_customer_communication_service", lambda _db: SimpleNamespace(send_email=fail_send))
    fields = {
        "csrf_token": "test-csrf", "draft_id": str(fixture.draft_id), "recipient_email": "recipient@example.test",
        "sender_email": "team@example.test", "subject": "Edited subject", "content": "<p>Edited content</p>",
        "mailbox_origin": "1",
    }
    if kind == "scheduled":
        fields["scheduled_at"] = "2050-01-01T12:00"
    path = f"/customers/{fixture.customer_id}/communications/emails" if kind.startswith("customer") else "/emails/send"
    stored_payload = fixture.db.get(HubMailboxEmail, fixture.draft_id).encrypted_payload_json
    response = fixture.client.post(
        path, headers={"Accept": "application/json"}, data=fields,
        files={"attachments": ("offer.pdf", b"%PDF-test", "application/pdf")}, follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json() == {"detail": message}
    assert "location" not in response.headers
    assert len(calls) == 1
    assert calls[0]["subject"] == "Edited subject"
    assert calls[0]["attachments"][0].content == b"%PDF-test"
    assert fixture.db.get(HubMailboxEmail, fixture.draft_id).encrypted_payload_json == stored_payload


def test_successful_delivery_returns_navigation_target_after_deleting_draft(delivery_client, monkeypatch):
    monkeypatch.setattr(HubMailboxService, "send_direct_email", lambda *_args, **_kwargs: SimpleNamespace(id=52))
    fixture = delivery_client
    response = fixture.client.post("/emails/send", headers={"Accept": "application/json"}, data={
        "csrf_token": "test-csrf", "draft_id": str(fixture.draft_id),
    })
    assert response.status_code == 200
    assert "folder=sent" in response.json()["redirect_url"]
    assert "selected=unassigned-52" in response.json()["redirect_url"]
    assert fixture.db.get(HubMailboxEmail, fixture.draft_id) is None


def test_failed_delivery_keeps_legacy_html_response_for_non_ajax_clients(delivery_client, monkeypatch):
    def fail_send(*_args, **_kwargs):
        raise ValueError("Delivery failed")

    monkeypatch.setattr(HubMailboxService, "send_direct_email", fail_send)
    response = delivery_client.client.post("/emails/send", data={"csrf_token": "test-csrf"}, follow_redirects=False)
    assert response.status_code == 303
    assert "email_state=error" in response.headers["location"]


def test_composer_keeps_all_inputs_and_folder_on_send_failure():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise the composer JavaScript")
    result = subprocess.run([node, "tests/js/email_delivery_failure.cjs"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_manual_draft_save_closes_only_after_confirmed_success():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise the composer JavaScript")
    result = subprocess.run([node, "tests/js/email_draft_save_close.cjs"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
