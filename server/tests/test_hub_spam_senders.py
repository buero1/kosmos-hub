import json
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_spam_sender import HubSpamSender
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_imap_import import HubMailboxImapImportService, _ParsedMessage
from app.services.hub_spam_senders import HubSpamSenderService
from app.services.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhookService


def _message(identity: str, sender: str) -> _ParsedMessage:
    return _ParsedMessage(
        identity=identity,
        payload={"from": [{"email": sender}], "direction": "inbound", "subject": identity},
        occurred_at=datetime(2026, 9, 14, tzinfo=UTC),
        is_unread=True,
        attachments=(),
    )


def test_spam_sender_survives_message_deletion_and_not_spam_removes_rule():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        mailbox = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        importer = HubMailboxImapImportService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        original = HubMailboxEmail(
            direction="inbound", fingerprint="a" * 64,
            encrypted_payload_json=cipher.encrypt(json.dumps({"from": [{"email": "SPAM@EXAMPLE.DE"}]})),
            received_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        db.add(original)
        db.flush()
        assert mailbox.apply_batch_action(keys=[f"unassigned-{original.id}"], action="move_spam") == 1
        assert db.scalar(select(HubSpamSender.email_address)) == "spam@example.de"

        mailbox.apply_batch_action(keys=[f"unassigned-{original.id}"], action="move_trash")
        mailbox.apply_batch_action(keys=[f"unassigned-{original.id}"], action="permanently_delete")
        assert db.scalar(select(HubSpamSender.email_address)) == "spam@example.de"

        assert importer._store_message(parsed=_message("second", "spam@example.de"))[0] == "imported"
        second = db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.source == "mittwald-imap"))
        assert second.mailbox_state == "spam"
        mailbox.apply_batch_action(keys=[f"unassigned-{second.id}"], action="restore")
        assert db.scalar(select(HubSpamSender.id)) is None
        assert second.mailbox_state == "active"

        assert importer._store_message(parsed=_message("third", "spam@example.de"))[0] == "imported"
        third = db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.fingerprint != second.fingerprint))
        assert third.mailbox_state == "active"


def test_sender_rules_only_match_exact_inbound_address_across_sources(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        rules = HubSpamSenderService(db=db)
        assert rules.block(direction="inbound", payload={"from": "Sender <Blocked@Example.DE>"})
        assert not rules.block(direction="inbound", payload={"from": "blocked@example.de"})
        assert not rules.is_blocked(direction="inbound", payload={"from": "other@example.de"})
        assert not rules.is_blocked(direction="outbound", payload={"from": "blocked@example.de"})

        customer = Customer(name="Kunde", zoho_id="zoho-1")
        db.add(customer)
        db.flush()
        importer = HubMailboxImapImportService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        monkeypatch.setattr(importer, "_matched_customers", lambda payload: (customer,))
        importer._store_message(parsed=_message("linked", "blocked@example.de"))
        linked = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.source == "mittwald-imap"))
        assert linked.mailbox_state == "spam"

        communications = CustomerCommunicationService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        communications._upsert_zoho_email(
            customer=customer,
            record={"id": "zoho-mail-1", "from": [{"email": "blocked@example.de"}], "direction": "inbound"},
            module="Accounts", record_id="zoho-1", synced_at=datetime(2026, 9, 14, tzinfo=UTC),
            known_emails={}, mark_new_emails_unread=True,
        )
        zoho_email = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-mail-1"))
        assert zoho_email.mailbox_state == "spam"

        webhook = ZohoEmailWorkflowWebhookService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        webhook._store_unassigned_email(
            payload={"from": [{"email": "blocked@example.de"}], "direction": "inbound"},
            received_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        unassigned = db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.source == "zoho-workflow"))
        assert unassigned.mailbox_state == "spam"

        assert rules.unblock_id(db.scalar(select(HubSpamSender.id))) == "blocked@example.de"
        assert rules.list_senders() == ()
