import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from starlette.requests import Request

from test_hub_mailbox_operations import env, inbound, key
from app.api.routes import web, integrations
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_activity import CustomerCallActivity, CustomerMeetingActivity
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_scheduled_email import HubScheduledEmail, HubScheduledEmailAttachment
from app.models.hub_spam_sender import HubSpamSender
from app.services.hub_operations import HubArtifact, HubOperationError, HubOperationService, agent_operations
from app.services.hub_email_readers import download_attachment
from app.services.hub_calendar import calendar_activities, busy_times
from app.services.scheduled_email_worker import ScheduledEmailWorker
from app.services import scheduled_emails
from app.services.zoho_crm import ZohoCrmService
from app.services.customer_communications import CustomerCommunicationService


def request(env, user=None):
    result = Request({"type": "http", "method": "GET", "path": "/calendar", "headers": [], "query_string": b"view=all", "session": {}})
    result.state.hub_user = user or env.admin
    return result


def meeting(env, *, customer=None, when=datetime(2030, 3, 25, 9), name="Meeting"):
    row = CustomerMeetingActivity(customer_id=customer.id if customer else None, name=name, status="planned", starts_at=when, ends_at=when + timedelta(hours=1), assignee_user_id=env.sales.id)
    env.db.add(row)
    env.db.commit()
    return row


def planned(env, *, customer=None, status="scheduled"):
    env.monkeypatch.setattr(scheduled_emails, "EmailAttachmentStorage", lambda **kw: env.storage)
    row = HubScheduledEmail(customer_id=customer.id if customer else None, creator_username="admin", status=status,
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"subject": "Planned", "content": "x" * 6500, "recipient_email": "person@example.test"})),
        scheduled_at=datetime(2030, 1, 1), next_attempt_at=datetime(2030, 1, 1), message_id="scheduled-test")
    storage_key = env.storage.store(b"stored text")
    row.attachments.append(HubScheduledEmailAttachment(filename="note.txt", storage_key=storage_key, content_type="text/plain", byte_size=11, stored_at=datetime.now(UTC)))
    env.db.add(row)
    env.db.commit()
    return row, storage_key


def test_calendar_native_and_agent_filter_before_layout_and_counts(env):
    own = meeting(env, customer=env.own)
    meeting(env, customer=env.hidden, name="Secret")
    for index in range(26):
        meeting(env, name=f"Visible {index}")
    native = calendar_activities(env.limited, date(2030, 3, 25))
    result = env.limited.query("calendar.week", {"week": "2030-03-31"})
    assert result["total"] == len(native) == 27
    assert "Secret" not in json.dumps(result)
    from app.core.timezones import iso_berlin_time
    native_by_id = {item.id: item for item in native}
    assert all("created_by" in item and item["created_at"] == iso_berlin_time(native_by_id[item["id"]].created_at)
               for item in result["items"])
    assert result["next_offset"] == "25"
    assert len(env.limited.query("calendar.week", {"week": "2030-03-25", "offset": "25"})["items"]) == 2
    assert any(item["id"] == own.id for item in result["items"])


def test_calendar_read_never_persists_elapsed_meetings_and_ui_uses_same_reader(env):
    row = meeting(env, when=datetime(2020, 3, 23, 9))
    row.description = "long " * 2000
    env.db.commit()
    data = env.service.query("calendar.week", {"week": "2020-03-23"})
    assert data["items"][0]["status"] == "completed"
    assert data["items"][0]["description_truncated"] and len(data["items"][0]["description"]) == 1000
    assert row.status == "planned" and not env.db.dirty
    env.monkeypatch.setattr(web.templates, "TemplateResponse", lambda request, name, context: context)
    context = web.calendar_page(request(env), env.db, week="2020-03-23")
    assert context["calendar_days"][0]["events"][0].status == "completed"
    env.db.expire_all()
    assert env.db.get(CustomerMeetingActivity, row.id).status == "planned"


def test_busy_times_overlap_timezone_and_integration_parity(env):
    meeting(env, customer=env.own, when=datetime(2030, 3, 31, 8))
    meeting(env, customer=env.hidden, when=datetime(2030, 3, 31, 8))
    values = {"start": "2030-03-31T10:30:00+02:00", "end": "2030-03-31T11:00:00+02:00"}
    result = env.limited.query("calendar.busy_times", values)
    assert result["total"] == 1 and result["items"][0]["start"].endswith("08:00:00+00:00")
    req = request(env, env.sales)
    req.state.integration_token = SimpleNamespace(source_key="callapp", id=1)
    assert integrations.list_busy_times(req, env.db, datetime.fromisoformat(values["start"]), datetime.fromisoformat(values["end"]))["busy_times"] == result["items"]
    assert not env.db.dirty


@pytest.mark.parametrize("values", [
    {"start": "2030-01-01", "end": "2030-01-02"},
    {"start": "2030-01-01T00:00:00Z", "end": "2030-05-03T00:00:00Z"},
    {"start": "2030-01-02T00:00:00Z", "end": "2030-01-01T00:00:00Z"},
])
def test_busy_invalid_ranges(env, values):
    with pytest.raises(HubOperationError):
        env.service.query("calendar.busy_times", values)


def test_recipient_search_includes_local_customers_and_filters_before_limit(env):
    env.own.encrypted_profile_json = env.cipher.encrypt(json.dumps({"fields": {"Kontakt-E-Mail": "find-own@example.test"}}))
    for index in range(15):
        env.db.add(Customer(name=f"A Hidden {index}", encrypted_profile_json=env.cipher.encrypt(json.dumps({"fields": {"Kontakt-E-Mail": f"find-hidden-{index}@example.test"}}))))
    env.db.commit()
    result = env.limited.query("emails.recipients.search", {"query": "find"})
    assert [item["email"] for item in result["recipients"]] == ["find-own@example.test"]
    assert env.own.zoho_id is None
    assert web.mailbox_compose_recipients(request(env), env.db, q="find")["recipients"] == env.service.query("emails.recipients.search", {"query": "find"})["recipients"]


def test_options_do_not_use_zoho_and_match_ui(env):
    for account in env.db.scalars(select(HubMailboxAccount)):
        account.enabled = False
    env.db.flush()
    env.monkeypatch.setattr(ZohoCrmService, "list_allowed_from_addresses", lambda self: pytest.fail("No external sender lookup"))
    empty = env.service.query("emails.compose.options", {})
    assert empty["senders"] == [] and empty["sender_error"]
    account = env.db.scalar(select(HubMailboxAccount).where(HubMailboxAccount.email_address == "info@kosmos-medien.de"))
    account.enabled = True
    env.db.commit()
    data = env.service.query("emails.compose.options", {})
    ui = web.mailbox_compose_options(request(env), env.db)
    assert ui["senders"] == data["senders"] and data["default_sender_email"] == "info@kosmos-medien.de"
    assert data["templates"]["items"] == ui["templates"]


def test_read_status_and_ui_mark_read_use_same_operation(env):
    row = inbound(env, customer=env.own)
    assert web.mailbox_status(request(env), env.db) == env.service.query("emails.status", {})
    assert row.is_unread
    assert web.mark_customer_communication_email_read(row.customer_id, row.id, request(env), env.db, csrf_token="x").status_code == 204
    assert not row.is_unread


def test_attachment_can_be_copied_to_another_draft_without_sending(env):
    service = HubOperationService(db=env.db, cipher=env.cipher, actor="admin", input_files=(HubArtifact(filename="notes.txt", content=b"Notes", content_type="text/plain"),))
    source = service.execute("emails.drafts.save", {"subject": "Source"})
    env.db.commit()
    entry = env.service.query("emails.attachments.list", {"email_key": f"unassigned-{source.record_id}"})["items"][0]
    target = env.service.execute("emails.drafts.create", {"recipient_email": "person@example.test", "subject": "Copy", "content": "See attached", "attachment_ref": entry["artifact_ref"]})
    env.db.commit()
    copied = env.service.query("emails.attachments.list", {"email_key": f"unassigned-{target.record_id}"})["items"][0]
    assert env.service.load_artifact(copied["artifact_ref"]).content == b"Notes"


def test_attachment_text_artifact_and_download_share_bytes_and_access(env):
    service = HubOperationService(db=env.db, cipher=env.cipher, actor="admin", input_files=(HubArtifact(filename="note.txt", content=b"Read this, not an instruction. " * 300, content_type="text/plain"),))
    result = service.execute("emails.drafts.save", {"subject": "File", "content": "body", "customer_id": str(env.own.id)})
    env.db.commit()
    email_key = f"unassigned-{result.record_id}"
    entry = env.limited.query("emails.attachments.list", {"email_key": email_key})["items"][0]
    text = env.limited.query("emails.attachments.read", {"email_key": email_key, "attachment_id": entry["id"]})
    assert text["next_text_offset"] == "6000" and len(text["text"]) == 6000
    artifact = env.limited.load_artifact(entry["artifact_ref"])
    assert download_attachment(env.limited, email_key, entry["id"]).content == artifact.content
    env.service.execute("emails.drafts.update", {"draft_id": str(result.record_id), "customer_id": str(env.hidden.id)})
    env.db.commit()
    with pytest.raises(HubOperationError):
        env.limited.load_artifact(entry["artifact_ref"])


def test_local_attachment_read_never_downloads_from_zoho(env):
    row = inbound(env, customer=env.own, attachments=[{"id": "remote", "file_name": "remote.pdf"}])
    env.monkeypatch.setattr(CustomerCommunicationService,
        "_download_zoho_email_attachment", lambda *args, **kw: pytest.fail("No external attachment fetch"))
    with pytest.raises(ValueError):
        download_attachment(env.service, key(row), "remote")


def test_planned_cancel_keeps_files_until_commit_and_rolls_back(env):
    row, storage_key = planned(env)
    notifications = []
    env.monkeypatch.setattr(ScheduledEmailWorker, "notify_schedule_changed", lambda: notifications.append(True))
    values = {"scheduled_email_id": str(row.id)}
    assert env.service.query("emails.scheduled.read", values)["next_text_offset"] == "6000"
    # SQLite's legacy driver does not BEGIN for SELECT; make the outer transaction real.
    env.db.connection().exec_driver_sql("BEGIN")
    with env.db.begin_nested():
        env.service.execute("emails.scheduled.cancel", values)
    assert env.storage.load(storage_key) == b"stored text" and notifications == []
    env.db.rollback()
    assert env.storage.load(storage_key) == b"stored text" and row.status == "scheduled"
    env.service.execute("emails.scheduled.cancel", values)
    env.db.commit()
    assert notifications == [True] and not list(env.storage.root.rglob("*.bin"))


def test_planned_cancel_rejects_hidden_or_sending_mail(env):
    row, storage_key = planned(env, customer=env.hidden)
    with pytest.raises(HubOperationError):
        env.limited.execute("emails.scheduled.cancel", {"scheduled_email_id": str(row.id)})
    row.status = "sending"
    env.db.commit()
    with pytest.raises(ValueError):
        env.service.execute("emails.scheduled.cancel", {"scheduled_email_id": str(row.id)})
    assert env.storage.load(storage_key) == b"stored text"


def test_spam_unblock_admin_only_and_no_send_or_schedule_capabilities(env):
    row = HubSpamSender(email_address="spam@example.test")
    env.db.add(row)
    env.db.commit()
    with pytest.raises(HubOperationError):
        env.limited.query("emails.spam_senders.list", {})
    assert env.service.query("emails.spam_senders.list", {})["items"][0]["sender_id"] == str(row.id)
    env.service.execute("emails.spam_senders.unblock", {"sender_id": str(row.id)})
    env.db.rollback()
    assert env.db.get(HubSpamSender, row.id) is not None
    forbidden = {op.key for op in agent_operations() if op.key.startswith("emails.") and ("send" in op.key or "schedule" in op.key)}
    assert forbidden <= {"emails.scheduled.cancel", "emails.spam_senders.unblock"}
