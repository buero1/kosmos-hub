import json
from pathlib import Path
import re
import shutil
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from mailbox_fixture_helpers import mailbox_account
from sqlalchemy.pool import StaticPool

from app.core.security import SecretCipher
from app.core.templates import create_templates
from app.db.base import Base
from app.models.hub_lead import HubLead
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_operations import HubOperationError, HubOperationService


@pytest.mark.parametrize("with_lead", [True, False])
def test_manual_draft_form_roundtrip_preserves_explicit_lead(monkeypatch, with_lead):
    from app.api.routes import web

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    monkeypatch.setattr(web, "get_secret_cipher", lambda: cipher)
    with Session(engine) as db:
        user = HubUser(username="draft-author", password_hash="x", role="admin")
        mailbox_account(db, SecretCipher("a" * 32), "team@example.test")
        lead = HubLead(encrypted_profile_json=cipher.encrypt(json.dumps({
            "fields": {"first_name": "Lea", "last_name": "Beispiel", "email": "lea@example.test"},
        })))
        db.add_all([user, lead])
        db.commit()
        app = FastAPI()
        app.add_api_route("/emails/drafts", web.save_mailbox_draft, methods=["POST"])
        app.add_api_route("/emails/drafts/{draft_id}/compose-context", web.mailbox_draft_compose_context)
        app.dependency_overrides[web.get_db] = lambda: db

        @app.middleware("http")
        async def signed_in_request(request, call_next):
            request.state.hub_user = user
            request.scope["session"] = {"csrf_token": "test-csrf"}
            return await call_next(request)

        fields = {
            "csrf_token": "test-csrf", "recipient_email": "lea@example.test",
            "sender_email": "team@example.test", "subject": "Angebot",
            "content": "<p>Unser Angebot.</p>",
        }
        if with_lead:
            fields["lead_id"] = str(lead.id)
        with TestClient(app) as client:
            response = client.post("/emails/drafts", data=fields)
            assert response.status_code == 200, response.text
            draft_id = response.json()["draft_id"]
            context = client.get(f"/emails/drafts/{draft_id}/compose-context").json()
            assert context["lead_id"] == (lead.id if with_lead else None)
            fields.update(draft_id=str(draft_id), lead_id=str(context["lead_id"] or ""), subject="Bearbeitet")
            assert client.post("/emails/drafts", data=fields).status_code == 200
            assert client.get(f"/emails/drafts/{draft_id}/compose-context").json()["lead_id"] == context["lead_id"]

        selected = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test").get_selected_message(
            folder="drafts", unread_only=False, selected_key=f"unassigned-{draft_id}",
        )
        assert selected.subject == "Bearbeitet"
        rendered = create_templates(directory="app/templates").get_template("emails_reading_pane.html").render(selected=selected)
        assert (f'<a href="/leads/{lead.id}">Lea Beispiel</a>' in rendered) == with_lead
        assert len(db.scalars(select(HubMailboxEmail)).all()) == 1
        stored = db.get(HubMailboxEmail, draft_id)
        assert stored.source == "hub-draft"
        assert "Unser Angebot" not in stored.encrypted_payload_json


def test_manual_draft_rejects_a_lead_outside_the_users_access():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        HubAccessControlService(db=db).save_role(
            role_key="draft-only", name="Drafts", description="",
            permissions={"emails": {"view": True, "create": True}, "leads": {"view": True, "scope": "none"}},
        )
        db.add(HubUser(username="restricted-author", password_hash="x", role="draft-only"))
        db.flush()
        with pytest.raises(HubOperationError, match="Der Lead ist nicht"):
            HubOperationService(db=db, cipher=SecretCipher("a" * 32), actor="restricted-author").execute(
                "emails.drafts.save", {"lead_id": "7", "subject": "Nicht erlaubt"},
            )
        assert db.scalars(select(HubMailboxEmail)).all() == []


def test_lead_trigger_and_both_composers_keep_the_lead_context():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise the composer JavaScript")
    templates = create_templates(directory="app/templates")
    lead_template = Path("app/templates/lead_detail.html").read_text(encoding="utf-8")
    trigger = re.search(r'<button[^>]+data-email-compose-open.*?</button>', lead_template).group()
    rendered = templates.env.from_string(trigger).render(detail={"lead": {"id": 42}}, lead_email="lea@example.test")
    assert 'data-email-compose-lead-id="42"' in rendered
    for template_path in ("partials/global_mailbox_composer.html", "emails.html"):
        template = Path("app/templates", template_path).read_text(encoding="utf-8")
        assert re.search(r'<input[^>]+name="lead_id"[^>]+data-email-compose-lead-id', template)
    completed = subprocess.run([node, "tests/js/lead_email_draft_context.cjs"], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
