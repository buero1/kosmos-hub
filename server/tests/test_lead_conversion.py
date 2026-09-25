"""Conversion is atomic, permission-aware and deliberately excludes historical mail."""
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.core.record_actor import record_actor_scope
from app.core.security import SecretCipher
from app.db.base import Base
from app.db.record_info_tracking import install_record_info_tracking
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_activity import CustomerCallActivity, CustomerCallReminder, CustomerMeetingActivity, CustomerTaskActivity
from app.models.customer_communication import CustomerZohoNote
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_lead import HubLead
from app.models.hub_lead_conversion import HubLeadConversion
from app.models.hub_lead_note import HubLeadNote
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.customer_directory import CustomerDirectoryService
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_conversion_info import conversion_info
from app.services.hub_email_associations import EmailAssociationService, index_email
from app.services.hub_lead_notes import HubLeadNoteService
from app.services.hub_leads import HubLeadError, HubLeadService
from app.services.hub_operations import HubOperationService
from app.services.hub_record_info import record_info
from app.services.hub_workflows import HubWorkflowService, LEAD_CUSTOMER_CONVERSION_WORKFLOW_KEY


@pytest.fixture
def env():
    engine = create_engine("sqlite://")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    install_record_info_tracking(factory)
    with factory() as db:
        cipher = SecretCipher("lead-conversion-test")
        user = HubUser(username="employee", first_name="Erika", last_name="Example", role="admin", password_hash="test")
        db.add(user)
        HubWorkflowService(db=db).ensure_default_workflows()
        db.commit()
        yield SimpleNamespace(db=db, cipher=cipher, user=user, service=HubLeadService(db=db, cipher=cipher))
    engine.dispose()


def fields(**overrides):
    values = dict(company="Example Company", salutation="Frau Dr.", first_name="Eva", last_name="Example",
                  email="eva@example.test", secondary_email="eva2@example.test", phone="089123", mobile="0170123",
                  phone_secondary="089456", street="Testweg 12", postal_code="80331", city="Muenchen", country="Deutschland",
                  website="https://example.test", industry="Bäckereien", position="Leitung", source="Test source")
    return {"lead_field__" + key: value for key, value in (values | overrides).items()}


def convert(env, lead, result="Vertrag"):
    with record_actor_scope(env.db, env.user.username):
        env.service.update_lead(lead_id=lead.id, submitted_values={"lead_field__lead_result": result})
    return env.db.scalar(select(HubLeadConversion).where(HubLeadConversion.lead_id == lead.id))


def profile(env, record):
    return json.loads(env.cipher.decrypt(record.encrypted_profile_json))["fields"]


@pytest.mark.parametrize("result", ["Vertrag", "Stattgefunden + Auftrag"])
def test_mapping_and_once_only_across_result_changes(env, result):
    lead = env.service.create_lead(submitted_values=fields())
    conversion = convert(env, lead, result)
    env.db.commit()
    customer = env.db.get(Customer, conversion.customer_id)
    contact = env.db.get(CustomerContact, conversion.contact_id)
    assert customer.name == "Example Company" and customer.zoho_status == "Neu"
    customer_values, contact_values = profile(env, customer), profile(env, contact)
    assert customer_values["Kunde Typ"] == "Kunde"
    assert customer_values["Rechnungsadresse - Straße"] == "Testweg 12"
    assert customer_values["Rechnungsadresse - PLZ"] == "80331"
    assert customer_values["Rechnungsadresse - Stadt"] == "Muenchen"
    assert customer_values["Website"] == "https://example.test"
    assert customer_values["Quelle"] == "Test source"
    assert customer_values["Auftragsdatum"] == profile(env, lead)["order_date"] == profile(env, lead)["billing_result_date"]
    assert contact.customer_id == customer.id
    assert contact_values["Name"] == "Eva Example" and contact_values["Anrede"] == "Frau" and contact_values["Titel"] == "Dr."
    assert contact_values["E-Mail"] == "eva@example.test" and contact_values["Zweite E-Mail-Adresse"] == "eva2@example.test"
    assert contact_values["Telefon alternativ"] == "089456" and contact_values["Funktion"] == "Leitung"
    assert conversion.actor_name == "Erika Example" and conversion.actor_user_id == env.user.id
    assert record_info(customer)["created_by"] == "Erika Example"
    assert env.db.get(HubLead, lead.id) is lead
    original_date = conversion.converted_at
    env.service.update_lead(lead_id=lead.id, submitted_values={"lead_field__company": "Changed Lead Company"})
    convert(env, lead, "Stattgefunden + Auftrag" if result == "Vertrag" else "Vertrag")
    env.service.update_lead(lead_id=lead.id, submitted_values={"lead_field__lead_result": "Rücktritt"})
    convert(env, lead)
    assert env.db.scalar(select(func.count()).select_from(Customer)) == 1
    assert env.db.scalar(select(func.count()).select_from(CustomerContact)) == 1
    assert customer.name == "Example Company" and conversion.converted_at == original_date
    # An ordinary customer edit must not erase workflow fields.
    directory = CustomerDirectoryService(db=env.db, cipher=env.cipher)
    detail = directory.get_detail(customer_id=customer.id)
    submitted = {"customer_field__" + field.key: field.form_value for field in detail.editable_profile_fields}
    directory.update_hub_customer(customer_id=customer.id, submitted_values=submitted)
    assert profile(env, customer)["Kunde Typ"] == "Kunde"
    assert profile(env, customer)["Auftragsdatum"] == customer_values["Auftragsdatum"]


@pytest.mark.parametrize("result", ["Vertrag", "Stattgefunden + Auftrag"])
@pytest.mark.parametrize("billing_date", ["", "2026-09-03"])
def test_conversion_keeps_manual_order_date_for_customer(env, result, billing_date):
    lead = env.service.create_lead(submitted_values=fields(order_date="2026-09-01", billing_result_date=billing_date))
    conversion = convert(env, lead, result)
    env.db.commit()
    customer = env.db.get(Customer, conversion.customer_id)
    assert profile(env, customer)["Auftragsdatum"] == profile(env, lead)["order_date"] == "2026-09-01"
    assert profile(env, lead)["billing_result_date"] == (billing_date or "2026-09-01")


def test_notes_are_independent_copies_with_original_author_and_dates(env):
    lead = env.service.create_lead(submitted_values=fields())
    notes = HubLeadNoteService(db=env.db, cipher=env.cipher)
    with record_actor_scope(env.db, env.user.username):
        view = notes.create_note(lead_id=lead.id, actor=env.user.username, title="Original", content="Keep me")
    original = env.db.get(HubLeadNote, view.id)
    old_date = datetime(2020, 1, 1, tzinfo=UTC)
    original.zoho_created_at = original.zoho_modified_at = old_date
    env.db.commit()
    conversion = convert(env, lead)
    copied = env.db.scalar(select(CustomerZohoNote))
    assert copied.customer_id == conversion.customer_id and copied.zoho_note_id is None
    assert copied.encrypted_payload_json == original.encrypted_payload_json
    assert copied.zoho_created_at.replace(tzinfo=UTC) == old_date and copied.zoho_modified_at.replace(tzinfo=UTC) == old_date
    assert record_info(copied)["created_by"] == record_info(original)["created_by"] == "Erika Example"
    notes.update_note(lead_id=lead.id, note_id=original.id, title="Changed", content="Only lead")
    assert json.loads(env.cipher.decrypt(copied.encrypted_payload_json))["Note_Content"] == "Keep me"
    convert(env, lead, "Stattgefunden + Auftrag")
    assert env.db.scalar(select(func.count()).select_from(CustomerZohoNote)) == 1


def test_activities_move_with_ids_assignees_and_reminders_intact(env):
    lead = env.service.create_lead(submitted_values=fields())
    now = datetime.now(UTC).replace(tzinfo=None)
    common = dict(lead_id=lead.id, name="Existing", created_by_username="employee", assignee_user_id=env.user.id)
    call = CustomerCallActivity(**common, starts_at=now, ends_at=now + timedelta(minutes=30))
    task = CustomerTaskActivity(**common, due_at=now, reminder_channel="popup", reminder_minutes_before=5)
    meeting = CustomerMeetingActivity(**common, starts_at=now, ends_at=now + timedelta(hours=1), status="completed")
    env.db.add_all([call, task, meeting])
    env.db.flush()
    reminder = CustomerCallReminder(call_id=call.id, channel="email", minutes_before=5, sort_order=0)
    popup = CustomerActivityReminderNotification(user_id=env.user.id, activity_kind="task", activity_id=task.id,
        reminder_key="primary", remind_at=now, snoozed_until=now + timedelta(minutes=5))
    queued = CustomerTaskEmailReminder(activity_kind="call", activity_id=call.id, reminder_key="primary",
        creator_username="employee", task_name=call.name, recipient_email="employee@example.test",
        sender_email="hub@example.test", scheduled_at=now, next_attempt_at=now, status="retrying", attempt_count=1)
    env.db.add_all([reminder, popup, queued])
    env.db.commit()
    ids = [call.id, task.id, meeting.id, reminder.id]
    conversion = convert(env, lead)
    assert [call.id, task.id, meeting.id, reminder.id] == ids
    for activity in (call, task, meeting):
        assert activity.customer_id == conversion.customer_id and activity.lead_id is None
        assert activity.assignee_user_id == env.user.id
    assert task.reminder_channel == "popup" and meeting.status == "completed" and reminder.call_id == call.id
    assert queued.customer_id == popup.customer_id == conversion.customer_id
    assert queued.status == "retrying" and queued.attempt_count == 1 and queued.next_attempt_at == now
    assert popup.snoozed_until == now + timedelta(minutes=5)


@pytest.mark.parametrize("missing", ["company", "last_name", "salutation"])
def test_missing_required_data_rolls_back_result_and_all_targets(env, missing):
    # Legacy data may omit fields that today's lead form requires.
    values = {key.removeprefix("lead_field__"): value for key, value in fields().items()}
    values[missing] = ""
    lead, _ = env.service.upsert_external_lead(source_system="import", source_external_id="missing", field_values=values)
    env.db.commit()
    with pytest.raises(HubLeadError):
        convert(env, lead)
    env.db.commit()
    assert profile(env, lead).get("lead_result") != "Vertrag"
    assert env.db.scalar(select(func.count()).select_from(Customer)) == 0
    assert env.db.scalar(select(func.count()).select_from(CustomerContact)) == 0


@pytest.mark.parametrize("duplicate", ["name", "domain", "contact"])
def test_possible_duplicates_are_not_overwritten_or_linked(env, duplicate):
    lead = env.service.create_lead(submitted_values=fields())
    if duplicate == "contact":
        existing = CustomerContact(encrypted_profile_json=env.cipher.encrypt(json.dumps({"fields": {"Name": "Other", "E-Mail": "EVA@example.test"}})))
    else:
        existing = Customer(name=" example company " if duplicate == "name" else "Other", website_domain="example.test" if duplicate == "domain" else None)
    env.db.add(existing)
    env.db.commit()
    with pytest.raises(HubLeadError, match="dublette"):
        convert(env, lead)
    env.db.commit()
    assert env.db.scalar(select(func.count()).select_from(HubLeadConversion)) == 0
    assert profile(env, lead).get("lead_result") != "Vertrag"


def test_workflow_disabled_and_old_imported_orders_are_not_backfilled(env):
    values = {key.removeprefix("lead_field__"): value for key, value in fields(lead_result="Vertrag").items()}
    lead, _ = env.service.upsert_external_lead(source_system="import", source_external_id="old", field_values=values)
    env.service.update_lead(lead_id=lead.id, submitted_values={"lead_field__city": "Berlin"})
    assert env.db.scalar(select(func.count()).select_from(Customer)) == 0
    workflow = next(row for row in HubWorkflowService(db=env.db).list_workflows() if row.workflow_key == LEAD_CUSTOMER_CONVERSION_WORKFLOW_KEY)
    workflow.is_enabled = False
    env.db.flush()
    convert(env, lead, "Stattgefunden + Auftrag")
    assert env.db.scalar(select(func.count()).select_from(Customer)) == 0


def test_failed_copy_rolls_back_even_if_caller_commits_after_error(env, monkeypatch):
    from app.services.hub_lead_conversion import LeadConversionService
    lead = env.service.create_lead(submitted_values=fields())
    env.db.commit()
    def fail(*args):
        raise ValueError("Injected failure")
    monkeypatch.setattr(LeadConversionService, "_copy_notes", fail)
    with pytest.raises(HubLeadError, match="Injected"):
        convert(env, lead)
    env.db.commit()
    for model in (Customer, CustomerContact, HubLeadConversion):
        assert env.db.scalar(select(func.count()).select_from(model)) == 0
    assert profile(env, lead).get("lead_result") != "Vertrag"


def test_role_rights_assignments_and_overview_links(env):
    access = HubAccessControlService(db=env.db)
    access.save_role(role_key="limited", name="Limited", description="", permissions={
        "leads": {"view": True, "edit": True, "scope": "assigned"},
        "customers": {"view": True, "create": True, "edit": True, "scope": "assigned"},
        "contacts": {"view": True, "create": False, "scope": "assigned"}})
    env.user.role = "limited"
    lead = env.service.create_lead(submitted_values=fields())
    access.assign_created_record(user=env.user, module_key="leads", record_id=lead.id)
    env.db.commit()
    operation = HubOperationService(db=env.db, cipher=env.cipher, actor=env.user.username)
    with pytest.raises(ValueError, match="Berechtigung|benötigt"):
        operation.execute("leads.update", {"lead_id": str(lead.id), "lead_result": "Vertrag"})
    permission = access.permission(role_key="limited", module_key="contacts")
    permission.can_create = True
    env.db.flush()
    operation.execute("leads.update", {"lead_id": str(lead.id), "lead_result": "Vertrag"})
    conversion = env.db.scalar(select(HubLeadConversion))
    customer = env.db.get(Customer, conversion.customer_id)
    assert access.can_access_record(user=env.user, module_key="customers", record_id=customer.id, action="edit")
    assert conversion_info(lead, env.user, env.cipher)["links"][0]["href"] == f"/customers/{customer.id}"
    assert conversion_info(customer, env.user, env.cipher)["links"][0]["href"] == f"/leads/{lead.id}"
    access.permission(role_key="limited", module_key="leads").can_view = False
    env.db.flush()
    assert not conversion_info(customer, env.user, env.cipher)["links"]


def stored_mail(env, date):
    payload = {"from": "hub@example.test", "to": "eva@example.test", "sent_time": date.isoformat(), "content": "test"}
    row = HubMailboxEmail(direction="outbound", fingerprint="test", received_at=date, created_at=date,
                          encrypted_payload_json=env.cipher.encrypt(json.dumps(payload)))
    env.db.add(row)
    env.db.flush()
    index_email(env.db, env.cipher, row)
    env.db.flush()
    return row, payload


def test_old_mail_stays_only_on_lead_even_when_imported_later(env):
    lead = env.service.create_lead(submitted_values=fields())
    old_date = datetime.now(UTC) - timedelta(days=1)
    old, old_payload = stored_mail(env, old_date)
    old_encrypted = old.encrypted_payload_json
    conversion = convert(env, lead)
    future = conversion.converted_at + timedelta(minutes=1)
    new, new_payload = stored_mail(env, future)
    delayed, delayed_payload = stored_mail(env, old_date)
    delayed.created_at = future
    env.db.flush()
    associations = EmailAssociationService(db=env.db, cipher=env.cipher)
    for module, identifier in (("customers", conversion.customer_id), ("contacts", conversion.contact_id)):
        assert [row.id for row in associations.records_for(module, identifier)] == [new.id]
    assert {row.id for row in associations.records_for("leads", lead.id)} == {old.id, new.id, delayed.id}
    assert {link.module for link in associations.links(old_payload, "outbound", record=old)} == {"leads"}
    assert {link.module for link in associations.links(new_payload, "outbound", record=new)} == {"leads", "customers", "contacts"}
    assert {link.module for link in associations.links(delayed_payload, "outbound")} == {"leads"}
    assert old.encrypted_payload_json == old_encrypted
    view = CustomerCommunicationService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test").get_view(customer_id=conversion.customer_id)
    assert len(view.emails) == 1


def test_deleting_targets_does_not_allow_reconversion(env):
    lead = env.service.create_lead(submitted_values=fields())
    conversion = convert(env, lead)
    customer = env.db.get(Customer, conversion.customer_id)
    env.db.delete(customer)
    env.db.commit()
    env.db.expire_all()
    convert(env, lead, "Stattgefunden + Auftrag")
    assert env.db.scalar(select(func.count()).select_from(Customer)) == 0
    assert env.db.scalar(select(func.count()).select_from(HubLeadConversion)) == 1


def test_running_reminder_blocks_conversion_without_changing_delivery(env):
    lead = env.service.create_lead(submitted_values=fields())
    now = datetime.now(UTC).replace(tzinfo=None)
    task = CustomerTaskActivity(lead_id=lead.id, name="Task", due_at=now)
    env.db.add(task)
    env.db.flush()
    job = CustomerTaskEmailReminder(task_id=task.id, activity_kind="task", activity_id=task.id,
        creator_username="employee", task_name="Task", recipient_email="employee@example.test", sender_email="hub@example.test",
        scheduled_at=now, next_attempt_at=now, status="sending")
    env.db.add(job)
    env.db.commit()
    with pytest.raises(HubLeadError, match="gerade versendet"):
        convert(env, lead)
    env.db.commit()
    assert task.lead_id == lead.id and task.customer_id is None and job.status == "sending"
    assert env.db.scalar(select(func.count()).select_from(Customer)) == 0


def test_overview_renders_conversion_date_actor_and_safe_links(env, monkeypatch):
    from app.core.templates import create_templates
    monkeypatch.setattr("app.core.security.get_secret_cipher", lambda: env.cipher)
    lead = env.service.create_lead(submitted_values=fields())
    conversion = convert(env, lead)
    customer = env.db.get(Customer, conversion.customer_id)
    customer.name = "<script>unsafe</script>"
    template = create_templates(directory="app/templates").env.from_string(
        '{% from "partials/hub_data_panel.html" import hub_data_panel with context %}{{ hub_data_panel(record) }}')
    html = template.render(record=lead, user=env.user)
    assert "Lead-Umwandlung" in html and "Umgewandelt am" in html and "Erika Example" in html
    assert f'href="/customers/{customer.id}"' in html and "&lt;script&gt;" in html and "<script>" not in html
    assert f'href="/leads/{lead.id}"' in template.render(record=customer, user=env.user)


@pytest.mark.parametrize("path", ["create", "external"])
def test_conversion_uses_shared_service_outside_normal_update(env, path):
    with record_actor_scope(env.db, env.user.username):
        if path == "create":
            lead = env.service.create_lead(submitted_values=fields(lead_result="Vertrag"))
        else:
            values = {key.removeprefix("lead_field__"): value for key, value in fields().items()}
            lead, _ = env.service.upsert_external_lead(source_system="test", source_external_id="transition", field_values=values)
            env.service.upsert_external_lead(source_system="test", source_external_id="transition", field_values={"lead_result": "Vertrag"})
    conversion = env.db.scalar(select(HubLeadConversion).where(HubLeadConversion.lead_id == lead.id))
    assert conversion.customer_id and conversion.contact_id and conversion.actor_user_id == env.user.id


def test_lead_deletion_keeps_customer_notes_and_transferred_activities(env):
    lead = env.service.create_lead(submitted_values=fields())
    HubLeadNoteService(db=env.db, cipher=env.cipher).create_note(lead_id=lead.id, actor="employee", content="Keep on customer")
    task = CustomerTaskActivity(lead_id=lead.id, name="Keep task")
    env.db.add(task)
    env.db.flush()
    conversion = convert(env, lead)
    env.service.delete_lead(lead_id=lead.id)
    env.db.commit()
    env.db.expire_all()
    assert conversion.lead_id is None
    assert env.db.get(Customer, conversion.customer_id)
    assert env.db.get(CustomerTaskActivity, task.id).customer_id == conversion.customer_id
    assert env.db.scalar(select(func.count()).select_from(CustomerZohoNote)) == 1
