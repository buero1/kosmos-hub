import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.core.templates import create_templates
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_email_address import HubEmailAddress
from app.models.hub_lead import HubLead
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_user import HubUser
from app.services.customer_communications import CustomerCommunicationService, CustomerCommunicationAttachmentUpload
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_email_associations import EmailAssociationService, index_email
from app.services.hub_lead_emails import HubLeadEmailService
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_imap_import import HubMailboxImapImportService
from app.services.hub_mailbox_transport import HubMailboxTransportService, HubMailboxTransportDelivery
from app.services.hub_operations import HubOperationService


@pytest.fixture
def env(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("email-association-tests")
    with Session(engine) as db:
        def profile(**fields):
            return cipher.encrypt(json.dumps({"fields": fields}))
        customer = Customer(name="Kunde", encrypted_profile_json=profile(**{"Kontakt-E-Mail": "kunde@example.test", "Zweite E-Mail-Adresse": "kunde2@example.test"}))
        lead = HubLead(encrypted_profile_json=profile(company="Lead", email="lead@example.test", secondary_email="lead2@example.test"))
        contact = CustomerContact(encrypted_profile_json=profile(**{"Name": "Kontakt", "E-Mail": "kontakt@example.test", "Zweite E-Mail-Adresse": "kontakt2@example.test"}))
        account = HubMailboxAccount(email_address="hub@example.test", display_name="Hub", username="hub@example.test", encrypted_password=cipher.encrypt("not-a-real-password"), verified_at=datetime.now(UTC))
        db.add_all([customer, lead, contact, account, HubUser(username="admin", role="admin", password_hash="x")])
        db.flush()
        storage = EmailAttachmentStorage(root=tmp_path, cipher=cipher, min_free_bytes=0)
        def mailbox(actor="admin"):
            return HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.test", actor=actor, attachment_storage=storage)
        yield SimpleNamespace(db=db, cipher=cipher, profile=profile, customer=customer, lead=lead, contact=contact, account=account, mailbox=mailbox, storage=storage)
    engine.dispose()


def stored(env, *, address="lead2@example.test", direction="outbound", state="active", cc=()):
    payload = {"subject": "Test association", "content": "<p>Test</p>",
               "from": address if direction == "inbound" else "hub@example.test",
               "to": "hub@example.test" if direction == "inbound" else address, "cc": cc}
    row = HubMailboxEmail(source="mittwald-imap", direction=direction, mailbox_state=state, is_unread=direction == "inbound", fingerprint="test", received_at=datetime.now(UTC), encrypted_payload_json=env.cipher.encrypt(json.dumps(payload)))
    env.db.add(row)
    env.db.flush()
    index_email(env.db, env.cipher, row)
    env.db.flush()
    return row


@pytest.mark.parametrize("module,prefix", [("leads", "lead"), ("customers", "kunde"), ("contacts", "kontakt")])
@pytest.mark.parametrize("suffix", ["", "2"])
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_primary_and_secondary_addresses_in_both_directions(env, module, prefix, suffix, direction):
    row = stored(env, address=f'"Test Name" <{prefix.upper()}{suffix}@EXAMPLE.TEST>', direction=direction)
    mailbox = env.mailbox()
    record = {"leads": env.lead, "customers": env.customer, "contacts": env.contact}[module]
    selected = mailbox.get_selected_message(folder="inbox" if direction == "inbound" else "sent", unread_only=False, selected_key=f"unassigned-{row.id}")
    assert [(link.module, link.id) for link in selected.record_links] == [(module, record.id)]
    assert mailbox.related_messages(module=module, record_id=record.id)[0].key == selected.key
    assert mailbox.get_folder_counts()["unassigned"] == 0
    assert not mailbox.get_folder_view(folder="unassigned", unread_only=False).messages
    rendered = create_templates(directory="app/templates").get_template("emails_reading_pane.html").render(selected=selected)
    assert f'href="/{module}/{record.id}"' in rendered
    if module == "leads":
        views = HubLeadEmailService(db=env.db, cipher=env.cipher).list_email_views(lead_id=record.id, actor="admin")
        assert views[0].mailbox_message.key == selected.key
    elif module == "customers":
        views = CustomerCommunicationService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test").get_view(customer_id=record.id, actor="admin")
        assert views.emails[0].mailbox_message.key == selected.key


def test_contact_also_links_customer_without_duplicate_messages(env):
    env.contact.customer_id = env.customer.id
    row = stored(env, address="kontakt2@example.test", cc=["kunde@example.test", "lead2@example.test"])
    message = env.mailbox().related_messages(module="customers", record_id=env.customer.id)
    assert len(message) == 1
    assert {link.module for link in message[0].record_links} == {"customers", "leads", "contacts"}
    assert message[0].key == f"unassigned-{row.id}"


@pytest.mark.parametrize("address", ["notlead@example.test", "lead2@example.test.other", "hub@example.test"])
def test_does_not_match_substrings_domains_or_own_mailbox(env, address):
    env.lead.encrypted_profile_json = env.profile(company="Lead", email="lead2@example.test", secondary_email="hub@example.test")
    row = stored(env, address=address)
    assert not env.mailbox().related_messages(module="leads", record_id=env.lead.id)
    assert not env.mailbox()._unassigned_message(row).record_links


def test_incoming_to_cc_does_not_assign_recipient_records(env):
    row = stored(env, address="stranger@example.test", direction="inbound", cc=["lead2@example.test"])
    assert not env.mailbox()._unassigned_message(row).record_links


@pytest.mark.parametrize("state", ["draft", "spam", "trash"])
def test_non_active_messages_do_not_appear_in_crm_panels(env, state):
    stored(env, state=state)
    assert not env.mailbox().related_messages(module="leads", record_id=env.lead.id)


def test_permissions_filter_messages_links_counts_and_agent_queries(env):
    access = HubAccessControlService(db=env.db)
    access.save_role(role_key="restricted-email", name="Restricted", description="", permissions={"emails": {"view": True}, "leads": {"view": True, "scope": "none"}, "customers": {"view": True, "scope": "all"}, "contacts": {"view": True, "scope": "none"}})
    env.db.add(HubUser(username="limited", password_hash="x", role="restricted-email"))
    row = stored(env)
    mailbox = env.mailbox("limited")
    assert mailbox.get_selected_message(folder="sent", unread_only=False, selected_key=f"unassigned-{row.id}") is None
    assert mailbox.get_folder_counts()["sent"] == 0
    assert not mailbox.related_messages(module="leads", record_id=env.lead.id)
    assert not mailbox.get_folder_view(folder="sent", unread_only=False).messages
    mixed = stored(env, cc=["kunde@example.test"])
    assert [link.module for link in env.mailbox("limited")._unassigned_message(mixed).record_links] == ["customers"]
    from app.services.hub_operation_email_relations import record_emails
    result = record_emails(HubOperationService(db=env.db, cipher=env.cipher, actor="admin"), {"module": "leads", "record_id": str(env.lead.id)})
    assert result["total"] == 2


def test_backfill_is_idempotent_and_record_changes_need_no_mail_rewrite(env):
    row = stored(env)
    encrypted = row.encrypted_payload_json
    index_email(env.db, env.cipher, row)
    env.db.flush()
    assert env.db.scalar(select(func.count()).select_from(HubEmailAddress)) == 1
    assert "lead2" not in env.db.scalar(select(HubEmailAddress.address_digest))
    env.lead.encrypted_profile_json = env.profile(company="Lead", email="different@example.test")
    env.db.flush()
    assert not env.mailbox().related_messages(module="leads", record_id=env.lead.id)
    env.lead.encrypted_profile_json = env.profile(company="Lead", email="lead2@example.test")
    env.db.flush()
    assert env.mailbox().related_messages(module="leads", record_id=env.lead.id)
    env.db.delete(env.lead)
    env.db.flush()
    assert not env.mailbox()._unassigned_message(row).record_links
    assert row.encrypted_payload_json == encrypted


def test_successful_direct_send_indexes_lead_and_preserves_attachment(env, monkeypatch):
    monkeypatch.setattr(HubMailboxTransportService, "send", lambda *args, **kwargs: HubMailboxTransportDelivery(message_id="<test-send@example.test>", sent_at=datetime.now(UTC)))
    row = env.mailbox().send_direct_email(sender_email="hub@example.test", recipient_email="lead2@example.test", subject="Unser Angebot", content="<p>Anbei</p>", cc_emails="", attachments=(CustomerCommunicationAttachmentUpload(filename="angebot.pdf", content=b"test-pdf", content_type="application/pdf"),))
    message = env.mailbox().related_messages(module="leads", record_id=env.lead.id)[0]
    attachment = message.attachments[0]
    assert env.mailbox().download_unassigned_attachment(email_id=row.id, attachment_id=attachment.id).content == b"test-pdf"
    html = create_templates(directory="app/templates").get_template("partials/associated_email_entry.html").render(email={"mailbox_message": message}, csrf_token="test")
    assert f'/emails/unassigned/{row.id}/attachments/{attachment.id}' in html
    assert "angebot.pdf" in html
    assert 'href="/emails?folder=' not in html
    assert "E-Mail-Vorschau anzeigen" in html
    assert 'data-email-preview-frame' in html
    imap = HubMailboxImapImportService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test")
    parsed = imap._parse_message(raw_message=b"From: hub@example.test\r\nTo: lead2@example.test\r\nMessage-ID: <test-send@example.test>\r\n\r\nTest", account=env.account, folder="Sent", uid="1", flags="")
    assert imap._store_message(parsed=parsed)[0] == "skipped"


def test_imap_index_is_written_for_leads_and_customer_contacts(env):
    env.contact.customer_id = env.customer.id
    imap = HubMailboxImapImportService(db=env.db, cipher=env.cipher, public_base_url="https://hub.test")
    for name in ("lead2", "kontakt2"):
        parsed = imap._parse_message(raw_message=f"From: {name}@example.test\r\nTo: hub@example.test\r\nMessage-ID: <{name}@example.test>\r\nSubject: Import\r\n\r\nTest".encode(), account=env.account, folder="INBOX", uid=name, flags="")
        assert imap._store_message(parsed=parsed)[0] == "imported"
    assert len(env.mailbox().related_messages(module="leads", record_id=env.lead.id)) == 1
    assert len(env.mailbox().related_messages(module="customers", record_id=env.customer.id)) == 1
    assert len(env.mailbox().related_messages(module="contacts", record_id=env.contact.id)) == 1
    assert env.db.scalar(select(func.count()).select_from(CustomerZohoEmail)) == 1
