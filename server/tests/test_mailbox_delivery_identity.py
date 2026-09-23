"""A sent message and each delivered mailbox copy have independent state."""
import json
from datetime import UTC, datetime
from email.message import EmailMessage
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.hub_email_associations import index_email
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_imap_import import HubMailboxImapImportService


@pytest.fixture
def env(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("mailbox-delivery-identity-tests")
    with Session(engine) as db:
        boxes = [HubMailboxAccount(email_address=f"{name}@example.test", display_name=name,
                                   username=name, encrypted_password=cipher.encrypt("test"))
                 for name in ("info", "staff", "websites")]
        db.add_all([*boxes, HubUser(username="admin", role="admin", password_hash="test")])
        db.flush()
        importer = HubMailboxImapImportService(db=db, cipher=cipher, public_base_url="https://hub.test")
        storage = EmailAttachmentStorage(root=tmp_path, cipher=cipher, min_free_bytes=0)
        importer.communications.attachment_storage = storage
        yield SimpleNamespace(db=db, cipher=cipher, boxes=boxes, importer=importer, storage=storage)
    engine.dispose()


def raw_message(sender="staff@example.test", recipients="info@example.test", attachment=False):
    message = EmailMessage()
    message["From"], message["To"] = sender, recipients
    message["Message-ID"] = "<delivery@example.test>"
    message["Subject"] = "Mailbox delivery regression"
    message.set_content("Hello")
    if attachment:
        message.add_attachment(b"Attachment", maintype="application", subtype="pdf", filename="example.pdf")
    return message.as_bytes()


def parsed(env, box=0, folder="INBOX", **kwargs):
    return env.importer._parse_message(raw_message=raw_message(**kwargs), account=env.boxes[box],
                                       folder=folder, uid="42", flags="")


def existing(env, *, source="hub-direct-send", direction="outbound", sender="staff@example.test",
             recipients="info@example.test", linked=False, mailbox=None):
    payload = {"message_id": "<delivery@example.test>", "subject": "Mailbox delivery regression",
               "from": sender, "to": recipients, "content": "Hello"}
    if mailbox:
        payload["mittwald_mailbox"] = mailbox
    fields = dict(source=source, direction=direction, is_unread=False,
                  encrypted_payload_json=env.cipher.encrypt(json.dumps(payload)))
    if linked:
        customer = Customer(name="External Customer", encrypted_profile_json=env.cipher.encrypt(
            json.dumps({"fields": {"E-Mail": "external@example.test"}})))
        env.db.add(customer)
        env.db.flush()
        row = CustomerZohoEmail(customer_id=customer.id, zoho_message_id="<delivery@example.test>",
                               zoho_synced_at=datetime.now(UTC), **fields)
    else:
        row = HubMailboxEmail(fingerprint=sha256(b"mittwald-imap:<delivery@example.test>").hexdigest(),
                              received_at=datetime.now(UTC), **fields)
    env.db.add(row)
    env.db.flush()
    index_email(env.db, env.cipher, row)
    env.db.flush()
    return row


def mailbox(env, box):
    return HubMailboxService(db=env.db, cipher=env.cipher, actor="admin", account_id=env.boxes[box].id,
                             public_base_url="https://hub.test", attachment_storage=env.storage)


@pytest.mark.parametrize(("source", "sender"), [("hub-direct-send", "staff@example.test"),
                                               ("hub-task-reminder", "info@example.test")])
def test_internal_send_and_self_reminder_are_received_once_with_independent_state(env, source, sender):
    sent = existing(env, source=source, sender=sender)
    encrypted_sent = sent.encrypted_payload_json
    message = parsed(env, sender=sender, attachment=True)
    assert env.importer._store_message(parsed=message) == ("imported", 1, 10)
    env.db.commit()
    assert env.importer._store_message(parsed=message) == ("skipped", 0, 0)
    received = env.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.direction == "inbound"))
    assert received.id != sent.id
    inbox = mailbox(env, 0).get_folder_view(folder="inbox", unread_only=False)
    assert [item.key for item in inbox.messages] == [f"unassigned-{received.id}"]
    assert received.is_unread and not sent.is_unread
    assert len(received.stored_attachments) == 1
    download = mailbox(env, 0).download_unassigned_attachment(
        email_id=received.id, attachment_id=received.stored_attachments[0].source_attachment_id)
    assert download.content == b"Attachment"
    mailbox(env, 0).apply_batch_action(action="move_trash", keys=[f"unassigned-{received.id}"])
    assert received.mailbox_state == "trash"
    assert sent.mailbox_state == "active" and sent.direction == "outbound"
    assert sent.encrypted_payload_json == encrypted_sent
    assert env.importer._store_message(parsed=message)[0] == "skipped"


def test_sent_folder_still_deduplicates_a_hub_sent_message(env):
    sent = existing(env)
    assert env.importer._store_message(parsed=parsed(env, box=1, folder="INBOX.Sent"))[0] == "skipped"
    assert list(env.db.scalars(select(HubMailboxEmail))) == [sent]


def test_same_message_delivered_to_two_accounts_is_available_in_each_account(env):
    recipients = "info@example.test, websites@example.test"
    existing(env, recipients=recipients)
    for box in (0, 2):
        message = parsed(env, box=box, recipients=recipients)
        assert env.importer._store_message(parsed=message)[0] == "imported"
        env.db.commit()
        assert env.importer._store_message(parsed=message)[0] == "skipped"
    info, websites = [mailbox(env, box).get_folder_view(folder="inbox", unread_only=False).messages for box in (0, 2)]
    assert len(info) == len(websites) == 1
    assert info[0].key != websites[0].key
    assert not mailbox(env, 1).get_folder_view(folder="inbox", unread_only=False).messages
    mailbox(env, 0).mark_unassigned_read(email_id=int(info[0].key.split("-")[1]))
    assert mailbox(env, 2).get_folder_view(folder="inbox", unread_only=True).messages


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_historical_customer_unique_key_does_not_block_another_delivery_or_its_crm_link(env, direction):
    first = existing(env, linked=True, source="mittwald-imap", direction=direction,
                     sender="external@example.test", mailbox="info@example.test")
    message = parsed(env, box=2, sender="external@example.test", recipients="info@example.test, websites@example.test")
    assert env.importer._store_message(parsed=message)[0] == "imported"
    env.db.commit()
    assert env.importer._store_message(parsed=message)[0] == "skipped"
    view = mailbox(env, 2).get_folder_view(folder="inbox", unread_only=False)
    assert len(view.messages) == 1
    related = mailbox(env, 2).related_messages(module="customers", record_id=first.customer_id)
    assert len(related) == 1 and related[0].key == view.messages[0].key
    assert list(env.db.scalars(select(CustomerZohoEmail))) == [first]


def test_legacy_inbound_copy_does_not_get_reimported(env):
    first = existing(env, source="mittwald-imap", direction="inbound", sender="external@example.test")
    message = parsed(env, sender="external@example.test")
    assert env.importer._store_message(parsed=message)[0] == "skipped"
    assert list(env.db.scalars(select(HubMailboxEmail))) == [first]


def test_moving_imported_message_does_not_change_its_delivery_identity(env):
    message = parsed(env)
    assert env.importer._store_message(parsed=message)[0] == "imported"
    row = env.db.scalar(select(HubMailboxEmail))
    mailbox(env, 0).apply_batch_action(action="move_sent", keys=[f"unassigned-{row.id}"])
    assert env.importer._store_message(parsed=message)[0] == "skipped"
    assert list(env.db.scalars(select(HubMailboxEmail))) == [row]
