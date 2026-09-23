import asyncio
import json
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.routes import web
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
from app.models.hub_lead_email import HubLeadEmail
from app.models.customer_activity import CustomerTaskActivity, CustomerCallActivity, CustomerMeetingActivity
from app.models.hub_scheduled_email import HubScheduledEmail
from app.models.hub_mailbox_email import HubMailboxEmail, HubMailboxAttachment
from app.models.hub_finance_offer import HubFinanceOffer
from app.services.hub_deletion import prepare_record_deletion, blocking_mail
from app.services.hub_leads import HubLeadService
from app.services.hub_operations import HubOperationError, HubOperationService, get_operation
from app.services.hub_mailbox import HubMailboxService
from app.services.scheduled_emails import ScheduledEmailService
from test_hub_finance_operations import env, req, values
from test_scheduled_emails import _sender, _storage
from test_email_delivery_response import delivery_client


def lead(env):
    env.db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    result = env.service.execute("leads.create", {"company": "Delete test", "email": "lead@example.test"})
    env.db.commit()
    return env.db.get(HubLead, result.record_id)


def planned(env, record, *, status="scheduled", linked=True, customer=False):
    row = HubScheduledEmail(lead_id=record.id if linked and not customer else None,
        customer_id=record.id if linked and customer else None, creator_username=env.user.username,
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"recipient_email": "lead@example.test", "content": "Keep"})),
        status=status, scheduled_at=datetime.now(UTC), next_attempt_at=datetime.now(UTC), message_id=f"{status}-{record.id}")
    env.db.add(row)
    env.db.commit()
    return row


def mailbox_message(env, record, *, state="draft", suffix="one"):
    data = {"recipient_lead_id": record.id, "recipient_key": f"lead:{record.id}", "context_module": "leads", "context_record_id": str(record.id),
            "recipient_email": "lead@example.test", "subject": "Keep subject", "content": "Keep content", "attachments": [{"id": "pdf", "name": "Keep.pdf"}]}
    row = HubMailboxEmail(source="hub-draft" if state == "draft" else "hub-direct-send", direction="outbound", mailbox_state=state,
        fingerprint=suffix, received_at=datetime.now(UTC), encrypted_payload_json=env.cipher.encrypt(json.dumps(data)))
    env.db.add(row)
    env.db.flush()
    attachment = HubMailboxAttachment(email_id=row.id, source_attachment_id="pdf", content_type="application/pdf",
        byte_size=3, storage_key="keep-" + suffix, stored_at=datetime.now(UTC))
    env.db.add(attachment)
    env.db.commit()
    return row, attachment, data


@pytest.mark.parametrize("status", ["scheduled", "retrying", "sending", "failed"])
@pytest.mark.parametrize("linked", [True, False])
def test_planned_mail_blocks_lead_delete_in_ui_and_shared_gateway(env, status, linked):
    row = lead(env)
    message, _, _ = mailbox_message(env, row)
    original = message.encrypted_payload_json
    scheduled = planned(env, row, status=status, linked=linked)
    preview = env.service.query("records.deletion_preview", {"target_path": f"/leads/{row.id}/delete"})
    assert preview["blockers"] and "geplante" in preview["blockers"][0]
    with pytest.raises(ValueError, match="geplante"):
        env.service.execute("leads.delete", {"lead_id": str(row.id)})
    env.db.rollback()
    response = asyncio.run(web.delete_lead_from_hub(row.id, req(env, {}), env.db))
    assert "fields=error" in response.headers["location"]
    assert env.db.get(HubLead, row.id) is not None
    assert message.encrypted_payload_json == original and scheduled.status == status


@pytest.mark.parametrize("status", ["sent", "cancelled"])
def test_deleted_lead_detaches_mail_but_preserves_content_and_attachments(env, status):
    row = lead(env)
    row_id = row.id
    draft, attachment, before = mailbox_message(env, row)
    sent, sent_attachment, _ = mailbox_message(env, row, state="active", suffix="two")
    scheduled = planned(env, row, status=status)
    offer_id = env.service.execute("finance.offers.create", {**values(env, "offers"), "customer_id": "", "contact_id": "", "lead_id": str(row_id)}).record_id
    note = HubLeadNote(lead_id=row_id, zoho_note_id="test", encrypted_payload_json=env.cipher.encrypt("{}"), zoho_imported_at=datetime.now(UTC))
    imported = HubLeadEmail(lead_id=row_id, zoho_message_id="test", zoho_record_id="test", encrypted_payload_json=env.cipher.encrypt("{}"), encrypted_header_json=env.cipher.encrypt("{}"), zoho_imported_at=datetime.now(UTC))
    task = CustomerTaskActivity(lead_id=row_id, name="Task")
    env.db.add_all([note, imported, task])
    env.db.commit()
    ids = (note.id, imported.id, task.id)
    preview = env.service.query("records.deletion_preview", {"target_path": f"/leads/{row_id}/delete"})
    assert "1 Notizen" in preview["deleted"] and "1 Aufgaben" in preview["deleted"] and not preview["blockers"]
    env.service.execute("leads.delete", {"lead_id": str(row_id)})
    env.db.commit()
    env.db.expire_all()
    assert env.db.get(HubLead, row_id) is None
    for model, pk in zip((HubLeadNote, HubLeadEmail, CustomerTaskActivity), ids):
        assert env.db.get(model, pk) is None
    for email, stored in ((draft, attachment), (sent, sent_attachment)):
        data = json.loads(env.cipher.decrypt(email.encrypted_payload_json))
        assert data == {**before, "recipient_lead_id": None, "recipient_key": "", "context_module": "", "context_record_id": ""}
        assert env.db.get(HubMailboxAttachment, stored.id) is not None
    offer = env.db.get(HubFinanceOffer, offer_id)
    assert offer.lead_id is None and len(offer.lines) == 2
    assert scheduled.lead_id is None


def test_failed_transaction_restores_lead_and_mail_links(env):
    row = lead(env)
    message, _, _ = mailbox_message(env, row)
    original = message.encrypted_payload_json
    row_id = row.id
    env.service.execute("leads.delete", {"lead_id": str(row_id)})
    env.db.rollback()
    assert env.db.get(HubLead, row_id) is not None
    assert message.encrypted_payload_json == original


def test_other_leads_mail_is_untouched_and_does_not_block(env):
    row = lead(env)
    other = lead(env)
    message, _, _ = mailbox_message(env, other)
    before = message.encrypted_payload_json
    planned(env, other)
    env.service.execute("leads.delete", {"lead_id": str(row.id)})
    env.db.commit()
    assert message.encrypted_payload_json == before


def test_customer_guard_reuses_same_scheduling_rule_without_enabling_customer_deletion(env):
    planned(env, env.customer, customer=True)
    with pytest.raises(ValueError, match="geplante"):
        prepare_record_deletion(env.db, env.cipher, kind="customers", record_id=env.customer.id)
    assert get_operation("customers.delete") is None


def test_preview_is_read_only_permission_checked_and_agent_notice_is_explicit(env):
    row = lead(env)
    scheduled = planned(env, row)
    other = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        other.query("records.deletion_preview", {"target_path": f"/leads/{row.id}/delete"})
    with pytest.raises(HubOperationError):
        env.service.query("records.deletion_preview", {"target_path": "/anything/1/delete"})
    assert "Aktivitaeten" in " ".join(get_operation("leads.delete").preview({"lead_id": str(row.id)}))
    env.service.query("records.deletion_preview", {"target_path": f"/leads/{row.id}/delete"})
    assert not env.db.new and not env.db.deleted and not env.db.dirty
    assert scheduled.status == "scheduled"


def test_scheduling_round_trips_explicit_lead_and_cancellation_unlocks_delete(env, tmp_path):
    row = lead(env)
    _sender(env.db, env.cipher, datetime.now(UTC))
    service = ScheduledEmailService(db=env.db, cipher=env.cipher, attachment_storage=_storage(tmp_path, env.cipher))
    scheduled = service.schedule(actor=env.user.username, lead_id=row.id, sender_email="info@kosmos-medien.de", recipient_email="lead@example.test",
        recipient_name="Lead", subject="Hello", content="<p>Keep</p>", cc_emails="", scheduled_at=datetime.now(UTC) + timedelta(days=1))
    env.db.commit()
    assert scheduled.lead_id == row.id
    assert service.get_compose_context(scheduled_email_id=scheduled.id)["lead_id"] == row.id
    with pytest.raises(ValueError, match="geplante"):
        HubLeadService(db=env.db, cipher=env.cipher).delete_lead(lead_id=row.id)
    service.cancel(scheduled_email_id=scheduled.id)
    env.db.commit()
    env.service.execute("leads.delete", {"lead_id": str(row.id)})
    env.db.commit()


def test_stale_lead_cannot_be_used_for_new_draft_or_schedule(env, tmp_path):
    row = lead(env)
    row_id = row.id
    env.service.execute("leads.delete", {"lead_id": str(row_id)})
    env.db.commit()
    with pytest.raises(ValueError, match="existiert"):
        ScheduledEmailService(db=env.db, cipher=env.cipher, attachment_storage=_storage(tmp_path, env.cipher)).schedule(
            actor=env.user.username, lead_id=row_id, sender_email="info@kosmos-medien.de", recipient_email="lead@example.test", recipient_name="", subject="Test",
            content="Text", cc_emails="", scheduled_at=datetime.now(UTC) + timedelta(days=1))
    with pytest.raises(ValueError):
        env.service.execute("emails.drafts.create", {"lead_id": str(row_id), "recipient_email": "lead@example.test", "subject": "Test", "content": "Test"})


@pytest.mark.parametrize("kind", ["articles", "offers", "orders", "invoices", "dunnings", "recurring-invoices"])
def test_finance_preview_matches_native_module_and_permissions(env, kind):
    record_id = env.service.execute(f"finance.{kind}.create", values(env, kind)).record_id
    env.db.commit()
    preview = env.service.query("records.deletion_preview", {"target_path": f"/finance/{kind}/{record_id}/delete"})
    assert preview["deleted"] and preview["retained"] and not preview["blockers"]
    assert not env.db.deleted and not env.db.dirty
    with pytest.raises(HubOperationError):
        HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username).query(
            "records.deletion_preview", {"target_path": f"/finance/{kind}/{record_id}/delete"})


def test_other_crm_previews_and_note_parent_validation(env):
    row = lead(env)
    note_id = env.service.execute("leads.notes.create", {"lead_id": str(row.id), "content": "Keep"}).record_id
    case_id = env.service.execute("cases.create", {"customer_id": str(env.customer.id), "case_field__case_origin": "Web"}).record_id
    task = CustomerTaskActivity(lead_id=row.id, name="Test")
    env.db.add(task)
    env.db.commit()
    for path in (f"/leads/{row.id}/notes/{note_id}/delete", f"/cases/{case_id}/delete", f"/contacts/{env.contact.id}/delete",
                 f"/leads/{row.id}/activities/tasks/{task.id}/delete"):
        preview = env.service.query("records.deletion_preview", {"target_path": path})
        assert preview["deleted"] and preview["retained"] and not preview["blockers"]
    other = lead(env)
    with pytest.raises(HubOperationError):
        env.service.query("records.deletion_preview", {"target_path": f"/leads/{other.id}/notes/{note_id}/delete"})


def test_real_email_route_and_mocked_delivery_preserve_lead_link(delivery_client, monkeypatch, tmp_path):
    from app.services.hub_mailbox_transport import HubMailboxTransportService, HubMailboxTransportDelivery
    fixture = delivery_client
    now = datetime(2040, 1, 1, 8, 0)
    clock = {"now": now}
    monkeypatch.setattr(ScheduledEmailService, "_utc_now", staticmethod(lambda: clock["now"]))
    monkeypatch.setattr(HubMailboxTransportService, "send", lambda self, **kw:
        HubMailboxTransportDelivery(message_id=kw["message_id"], sent_at=clock["now"].replace(tzinfo=UTC)))
    row = HubLead(encrypted_profile_json=fixture.cipher.encrypt(json.dumps({"fields": {"email": "lead@example.test"}})))
    fixture.db.add(row)
    _sender(fixture.db, fixture.cipher, now)
    fixture.db.commit()
    response = fixture.client.post("/emails/send", headers={"Accept": "application/json"}, data={
        "csrf_token": "test-csrf", "lead_id": str(row.id), "sender_email": "info@kosmos-medien.de",
        "recipient_email": "lead@example.test", "subject": "Keep", "content": "<p>Keep</p>", "scheduled_at": "2040-01-01T10:00"})
    assert response.status_code == 200 and "folder=planned" in response.json()["redirect_url"]
    scheduled = fixture.db.scalar(select(HubScheduledEmail))
    assert scheduled.lead_id == row.id
    with pytest.raises(ValueError, match="geplante"):
        HubLeadService(db=fixture.db, cipher=fixture.cipher).delete_lead(lead_id=row.id)
    fixture.db.rollback()
    clock["now"] = now + timedelta(hours=2)
    result = ScheduledEmailService(db=fixture.db, cipher=fixture.cipher, attachment_storage=_storage(tmp_path, fixture.cipher)).process_due()
    assert result.sent == 1
    sent = fixture.db.get(HubMailboxEmail, scheduled.mailbox_email_id)
    assert json.loads(fixture.cipher.decrypt(sent.encrypted_payload_json))["recipient_lead_id"] == row.id
    HubLeadService(db=fixture.db, cipher=fixture.cipher).delete_lead(lead_id=row.id)
    fixture.db.commit()
    assert json.loads(fixture.cipher.decrypt(sent.encrypted_payload_json))["recipient_lead_id"] is None
