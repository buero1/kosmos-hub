import json
from html import escape
import shutil
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from mailbox_fixture_helpers import mailbox_account

from app.core.security import SecretCipher
from app.core.templates import create_templates
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_lead import HubLead
from app.models.hub_mailbox_email import HubMailboxAttachment, HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.customer_communications import CustomerCommunicationActionResult
from app.services.email_attachment_storage import EmailAttachmentStorage, EmailAttachmentStorageError
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_operations import HubArtifact, HubOperationService


@pytest.fixture
def drafts(monkeypatch, tmp_path):
    from app.api.routes import web
    from app.services import customer_communications

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    storage = EmailAttachmentStorage(root=tmp_path / "attachments", cipher=cipher, min_free_bytes=0)
    monkeypatch.setattr(customer_communications, "EmailAttachmentStorage", lambda **kwargs: storage)
    monkeypatch.setattr(web, "get_secret_cipher", lambda: cipher)
    with Session(engine, expire_on_commit=False, autoflush=False) as db:
        user = HubUser(username="draft-author", password_hash="x", role="admin")
        customer = Customer(name="Example")
        lead = HubLead(encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"last_name": "Example"}})))
        db.add_all([user, customer, lead])
        mailbox_account(db, cipher)
        db.commit()
        app = FastAPI()
        app.add_api_route("/emails/drafts", web.save_mailbox_draft, methods=["POST"])
        app.add_api_route("/emails/drafts/{draft_id}/compose-context", web.mailbox_draft_compose_context)
        app.add_api_route("/emails/unassigned/{email_id}/attachments/{attachment_id}", web.download_unassigned_mailbox_attachment)
        app.add_api_route("/emails/send", web.send_direct_mailbox_email, methods=["POST"])
        app.add_api_route("/customers/{customer_id}/communications/emails", web.send_customer_communication_email, methods=["POST"])
        app.dependency_overrides[web.get_db] = lambda: db

        @app.middleware("http")
        async def authenticated(request, call_next):
            request.state.hub_user = user
            request.scope["session"] = {"csrf_token": "csrf"}
            return await call_next(request)

        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client, db=db, cipher=cipher, storage=storage, customer=customer, lead=lead,
                mailbox=HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test"),
            )


def save(drafts, *, files=(), **fields):
    return drafts.client.post("/emails/drafts", data={"csrf_token": "csrf", **fields}, files=files)


def uploads(*names):
    return [("attachments", (name, b"private file: " + name.encode(), "application/pdf")) for name in names]


@pytest.mark.parametrize("link", ["lead", "customer", "none"])
def test_upload_save_reopen_retry_and_remove(drafts, link):
    fields = {"retained_attachment_ids": "[]"}
    if link == "lead":
        fields["lead_id"] = str(drafts.lead.id)
    elif link == "customer":
        fields["recipient_customer_id"] = str(drafts.customer.id)
    response = save(drafts, files=uploads("offer.pdf", "terms.pdf"), **fields)
    assert response.status_code == 200, response.text
    payload = response.json()
    draft_id = payload["draft_id"]
    ids = payload["uploaded_attachment_ids"]
    assert len(set(ids)) == 2
    context = drafts.client.get(f"/emails/drafts/{draft_id}/compose-context").json()
    assert context["attachments"] == payload["attachments"]
    assert context["lead_id"] == (drafts.lead.id if link == "lead" else None)
    assert context["customer_id"] == (drafts.customer.id if link == "customer" else None)
    assert [item.content for item in drafts.mailbox.draft_attachments(draft_id=draft_id)] == [
        b"private file: offer.pdf", b"private file: terms.pdf",
    ]
    rows = drafts.db.scalars(select(HubMailboxAttachment)).all()
    assert all(b"private file" not in path.read_bytes() for path in drafts.storage.root.rglob("*.bin"))
    # Retrying the same multipart request after a lost response must not duplicate files.
    response = save(drafts, draft_id=str(draft_id), files=uploads("offer.pdf", "terms.pdf"), **fields)
    assert response.status_code == 200, response.text
    assert response.json()["attachments"] == payload["attachments"]
    assert len(drafts.db.scalars(select(HubMailboxAttachment)).all()) == 2
    # Older/text-only callers (including the agent) preserve the existing selection.
    assert len(save(drafts, draft_id=str(draft_id), subject="Edited").json()["attachments"]) == 2
    response = save(drafts, draft_id=str(draft_id), retained_attachment_ids=json.dumps([ids[1]]))
    assert response.status_code == 200
    assert response.json()["attachments"] == [payload["attachments"][1]]
    assert len(list(drafts.storage.root.rglob("*.bin"))) == 1
    assert drafts.storage.load(rows[1].storage_key) == b"private file: terms.pdf"
    response = save(drafts, draft_id=str(draft_id), retained_attachment_ids="[]")
    assert response.json()["attachments"] == []
    assert drafts.mailbox.draft_attachments(draft_id=draft_id) == ()
    assert list(drafts.storage.root.rglob("*.bin")) == []


@pytest.mark.parametrize("customer", [False, True])
@pytest.mark.parametrize("success", [False, True])
def test_send_uses_only_retained_and_new_files_without_real_delivery(drafts, monkeypatch, customer, success):
    from app.api.routes import web

    payload = save(drafts, files=uploads("offer.pdf", "removed.pdf")).json()
    draft_id = payload["draft_id"]
    ids = payload["uploaded_attachment_ids"]
    sent = []

    def send_email(*args, **kwargs):
        sent.extend(kwargs["attachments"])
        if not success:
            raise ValueError("Recipient rejected")
        return CustomerCommunicationActionResult(True, "Sent", 72) if customer else SimpleNamespace(id=72)

    monkeypatch.setattr(HubMailboxService, "send_direct_email", send_email)
    monkeypatch.setattr(web, "_customer_communication_service", lambda db: SimpleNamespace(send_email=send_email))
    route = f"/customers/{drafts.customer.id}/communications/emails" if customer else "/emails/send"
    response = drafts.client.post(route, headers={"Accept": "application/json"}, data={
        "csrf_token": "csrf", "draft_id": str(draft_id), "mailbox_origin": "1",
        "retained_attachment_ids": json.dumps([ids[0]]),
    }, files=uploads("new.pdf"))
    assert response.status_code == (200 if success else 400), response.text
    assert sorted(item.filename for item in sent) == ["new.pdf", "offer.pdf"]
    assert all(item.content == b"private file: " + item.filename.encode() for item in sent)
    if success:
        assert drafts.db.get(HubMailboxEmail, draft_id) is None
        assert list(drafts.storage.root.rglob("*.bin")) == []
    else:
        assert len(drafts.mailbox.draft_attachments(draft_id=draft_id)) == 2
        assert len(list(drafts.storage.root.rglob("*.bin"))) == 2


def test_unknown_attachment_ids_and_invalid_selection_do_not_change_draft(drafts):
    first = save(drafts, files=uploads("first.pdf")).json()
    second = save(drafts, files=uploads("second.pdf")).json()
    for selection in [json.dumps(second["uploaded_attachment_ids"]), "{}", "null", "[1]", "invalid"]:
        response = save(drafts, draft_id=str(first["draft_id"]), retained_attachment_ids=selection)
        assert response.status_code == 400
    assert len(drafts.mailbox.draft_attachments(draft_id=first["draft_id"])) == 1
    assert len(list(drafts.storage.root.rglob("*.bin"))) == 2


def test_draft_download_returns_original_file_and_rejects_other_draft_attachment(drafts):
    first = save(drafts, files=uploads("offer.pdf")).json()
    second = save(drafts, files=uploads("other.pdf")).json()
    url = f'/emails/unassigned/{first["draft_id"]}/attachments/{first["uploaded_attachment_ids"][0]}'
    response = drafts.client.get(url)
    assert response.status_code == 200
    assert response.content == b"private file: offer.pdf"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''offer.pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    wrong_url = f'/emails/unassigned/{first["draft_id"]}/attachments/{second["uploaded_attachment_ids"][0]}'
    assert drafts.client.get(wrong_url).status_code == 404
    save(drafts, draft_id=str(first["draft_id"]), retained_attachment_ids="[]")
    assert drafts.client.get(url).status_code == 404


@pytest.mark.parametrize("with_attachments", [False, True])
@pytest.mark.parametrize("with_content", [False, True])
def test_draft_reading_pane_shows_downloads_below_preview(drafts, with_attachments, with_content):
    filenames = ("offer.pdf", "terms & <details>.pdf") if with_attachments else ()
    payload = save(
        drafts, subject="Draft preview", content="<p>Draft message</p>" if with_content else "",
        files=uploads(*filenames),
    ).json()
    selected = drafts.mailbox.get_selected_message(
        folder="drafts", unread_only=False, selected_key=f'unassigned-{payload["draft_id"]}',
    )
    rendered = create_templates(directory="app/templates").get_template("emails_reading_pane.html").render(selected=selected)
    assert "data-mailbox-draft-open" in rendered
    assert ('class="communication-attachments"' in rendered) == with_attachments
    for attachment in payload["attachments"]:
        url = f'/emails/unassigned/{payload["draft_id"]}/attachments/{attachment["id"]}'
        filename = escape(attachment["filename"])
        assert f'<a href="{url}" download="{filename}">{filename}</a>' in rendered
        assert drafts.client.get(url).status_code == 200
    if with_attachments:
        assert rendered.index('class="communication-attachments"') > rendered.index("data-mailbox-draft-open")
        if with_content:
            assert rendered.index('class="communication-attachments"') > rendered.index("</iframe>")
        assert "terms & <details>.pdf" not in rendered


def test_cumulative_attachment_limits_and_empty_upload(drafts):
    first = save(drafts, files=uploads(*(f"{i}.pdf" for i in range(20)))).json()
    response = save(drafts, draft_id=str(first["draft_id"]), files=uploads("extra.pdf"))
    assert response.status_code == 400 and "20" in response.json()["detail"]
    row = drafts.db.scalars(select(HubMailboxAttachment)).first()
    row.byte_size = 50 * 1024 * 1024
    drafts.db.commit()
    response = save(drafts, draft_id=str(first["draft_id"]), retained_attachment_ids=json.dumps([row.source_attachment_id]), files=uploads("large.pdf"))
    assert response.status_code == 400 and "50 MB" in response.json()["detail"]
    response = save(drafts, files=[("attachments", ("empty.pdf", b"", "application/pdf"))])
    assert response.status_code == 400
    assert len(drafts.db.scalars(select(HubMailboxEmail)).all()) == 1


def test_storage_failure_keeps_existing_draft_and_cleans_new_files(drafts, monkeypatch):
    first = save(drafts, subject="Original", files=uploads("original.pdf")).json()
    original_store = drafts.storage.store
    calls = []

    def fail_second(content):
        calls.append(content)
        if len(calls) == 2:
            raise EmailAttachmentStorageError("Storage full")
        return original_store(content)

    monkeypatch.setattr(drafts.storage, "store", fail_second)
    response = save(drafts, draft_id=str(first["draft_id"]), subject="Changed", retained_attachment_ids="[]", files=uploads("one.pdf", "two.pdf"))
    assert response.status_code == 400
    context = drafts.mailbox.get_draft_compose_context(draft_id=first["draft_id"])
    assert context["subject"] == "Original"
    assert context["attachments"] == first["attachments"]
    assert len(list(drafts.storage.root.rglob("*.bin"))) == 1


@pytest.mark.parametrize("nested", [False, True])
def test_database_rollback_keeps_removed_files_and_cleans_uploads(drafts, nested):
    first = save(drafts, files=uploads("original.pdf")).json()
    transaction = drafts.db.begin_nested() if nested else None
    service = HubOperationService(db=drafts.db, cipher=drafts.cipher, actor="draft-author", input_files=(
        HubArtifact(filename="new.pdf", content_type="application/pdf", content=b"new file"),
    ))
    service.execute("emails.drafts.save", {"draft_id": str(first["draft_id"]), "retained_attachment_ids": "[]"})
    assert len(list(drafts.storage.root.rglob("*.bin"))) == 2
    if transaction:
        transaction.rollback()
        drafts.db.commit()
    else:
        drafts.db.rollback()
    assert len(list(drafts.storage.root.rglob("*.bin"))) == 1
    assert drafts.mailbox.draft_attachments(draft_id=first["draft_id"])[0].content == b"private file: original.pdf"


def test_committed_savepoint_does_not_delete_files_before_outer_commit(drafts):
    first = save(drafts, files=uploads("original.pdf")).json()
    with drafts.db.begin_nested():
        HubOperationService(db=drafts.db, cipher=drafts.cipher, actor="draft-author").execute(
            "emails.drafts.save", {"draft_id": str(first["draft_id"]), "retained_attachment_ids": "[]"},
        )
    assert len(list(drafts.storage.root.rglob("*.bin"))) == 1
    drafts.db.commit()
    assert list(drafts.storage.root.rglob("*.bin")) == []


def test_composer_attachment_save_and_inflight_edits():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for composer tests")
    result = subprocess.run([node, "tests/js/email_draft_attachments.cjs"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
