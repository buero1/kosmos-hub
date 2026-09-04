import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.main import _is_public_hub_path
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.customer_contact import CustomerContact
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhook
from app.models.zoho_email_workflow_delivery import ZohoEmailWorkflowDelivery
from app.services.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhookService


class FakeCommunicationService:
    def __init__(self) -> None:
        self.customer_ids: list[int] = []
        self.mark_new_emails_unread: list[bool] = []

    def sync_customer(self, *, customer_id: int, mark_new_emails_unread: bool = False) -> None:
        self.customer_ids.append(customer_id)
        self.mark_new_emails_unread.append(mark_new_emails_unread)


def test_only_the_generated_webhook_receiver_url_is_public():
    assert _is_public_hub_path("/account/zoho/email-workflow-webhook/receive/a-secret") is True
    assert _is_public_hub_path("/account/zoho/email-workflow-webhook/token") is False


def test_manual_email_webhook_queues_native_zoho_email_fields_before_syncing_the_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Example GmbH", zoho_id="zoho-account-1")
        contact = CustomerContact(
            customer=customer,
            zoho_id="zoho-contact-1",
            encrypted_profile_json="encrypted-contact-profile",
        )
        db.add_all([customer, contact])
        db.commit()

        communications = FakeCommunicationService()
        service = ZohoEmailWorkflowWebhookService(
            db=db,
            cipher=SecretCipher("a" * 32),
            public_base_url="https://hub.example.test",
            communication_service=communications,
        )
        token = service.rotate_token()

        assert service.endpoint == "https://hub.example.test/account/zoho/email-workflow-webhook/receive"
        assert service.receive(token="wrong", payload={"id": "email-1"}) is False
        assert service.receive(
            token=token,
            payload={"id": "email-1", "Entity_Id": "zoho-contact-1", "Module": "Contacts"},
        ) is True

        webhook = db.scalar(select(ZohoEmailWorkflowWebhook))
        assert webhook is not None
        delivery = db.scalar(select(ZohoEmailWorkflowDelivery))
        assert delivery is not None
        assert delivery.status == "pending"
        assert communications.customer_ids == []
        assert webhook.last_received_at is not None
        assert webhook.last_imported_at is None
        assert webhook.last_error is None
        assert webhook.encrypted_last_payload_json is not None
        assert "zoho-contact-1" not in webhook.encrypted_last_payload_json

        assert service.process_next_delivery() == "succeeded"
        assert communications.customer_ids == [customer.id]
        assert communications.mark_new_emails_unread == [True]
        assert delivery.status == "synced"
        assert webhook.last_imported_at is not None


def test_manual_email_webhook_matches_an_inbound_sender_to_one_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(name="Example GmbH", zoho_id="zoho-account-1")
        contact = CustomerContact(
            customer=customer,
            zoho_id="zoho-contact-1",
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"E-Mail": "kunde@example.de"}})),
        )
        db.add_all([customer, contact])
        db.commit()

        communications = FakeCommunicationService()
        service = ZohoEmailWorkflowWebhookService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            communication_service=communications,
        )
        token = service.rotate_token()

        assert service.receive(
            token=token,
            payload={"betreff": "Neue Anfrage", "absender": "Kunde <kunde@example.de>"},
        ) is True
        assert service.process_next_delivery() == "succeeded"

        assert communications.customer_ids == [customer.id]
        assert communications.mark_new_emails_unread == [True]


def test_manual_email_webhook_stores_an_unknown_sender_in_the_hub_mailbox():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        communications = FakeCommunicationService()
        service = ZohoEmailWorkflowWebhookService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            communication_service=communications,
        )
        token = service.rotate_token()

        assert service.receive(
            token=token,
            payload={"betreff": "Neue Anfrage", "absender": "Unknown <unknown@example.de>"},
        ) is True
        assert service.process_next_delivery() == "succeeded"

        mailbox_email = db.scalar(select(HubMailboxEmail))
        assert mailbox_email is not None
        assert mailbox_email.direction == "inbound"
        assert mailbox_email.is_unread is True
        assert communications.customer_ids == []


def test_manual_email_webhook_stores_an_unknown_zoho_contact_in_the_hub_mailbox():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        communications = FakeCommunicationService()
        service = ZohoEmailWorkflowWebhookService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            communication_service=communications,
        )
        token = service.rotate_token()

        assert service.receive(
            token=token,
            payload={
                "Entity_Id": "new-zoho-contact",
                "Module": "Contacts",
                "absender": "New contact <unknown@example.de>",
            },
        ) is True
        assert service.process_next_delivery() == "succeeded"

        assert db.scalar(select(HubMailboxEmail)) is not None
        assert communications.customer_ids == []


def test_manual_email_webhook_matches_an_outbound_recipient_to_one_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(name="Example GmbH", zoho_id="zoho-account-1")
        contact = CustomerContact(
            customer=customer,
            zoho_id="zoho-contact-1",
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"E-Mail": "kunde@example.de"}})),
        )
        db.add_all([customer, contact])
        db.commit()

        communications = FakeCommunicationService()
        service = ZohoEmailWorkflowWebhookService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            communication_service=communications,
        )
        token = service.rotate_token()

        assert service.receive(
            token=token,
            payload={"absender": "Hub <team@example.de>", "empfaenger": "Kunde <kunde@example.de>"},
        ) is True
        assert service.process_next_delivery() == "succeeded"

        assert communications.customer_ids == [customer.id]
        assert communications.mark_new_emails_unread == [True]


def test_manual_email_webhook_syncs_every_customer_with_a_matching_contact_address():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        first_customer = Customer(name="First GmbH", zoho_id="zoho-account-1")
        second_customer = Customer(name="Second GmbH", zoho_id="zoho-account-2")
        db.add_all(
            [
                first_customer,
                second_customer,
                CustomerContact(
                    customer=first_customer,
                    zoho_id="zoho-contact-1",
                    encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"E-Mail": "shared@example.de"}})),
                ),
                CustomerContact(
                    customer=second_customer,
                    zoho_id="zoho-contact-2",
                    encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"E-Mail": "shared@example.de"}})),
                ),
            ]
        )
        db.commit()

        communications = FakeCommunicationService()
        service = ZohoEmailWorkflowWebhookService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            communication_service=communications,
        )
        token = service.rotate_token()

        assert service.receive(token=token, payload={"absender": "Shared <shared@example.de>"}) is True
        assert service.process_next_delivery() == "succeeded"

        assert communications.customer_ids == [first_customer.id, second_customer.id]
        assert communications.mark_new_emails_unread == [True, True]


def test_a_zoho_message_id_can_be_stored_once_per_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first_customer = Customer(name="First GmbH", zoho_id="zoho-account-1")
        second_customer = Customer(name="Second GmbH", zoho_id="zoho-account-2")
        db.add_all(
            [
                first_customer,
                second_customer,
                CustomerZohoEmail(
                    customer=first_customer,
                    zoho_message_id="zoho-email-1",
                    source="zoho",
                    direction="inbound",
                    encrypted_payload_json="encrypted-email",
                ),
                CustomerZohoEmail(
                    customer=second_customer,
                    zoho_message_id="zoho-email-1",
                    source="zoho",
                    direction="inbound",
                    encrypted_payload_json="encrypted-email",
                ),
            ]
        )
        db.commit()

        assert len(db.scalars(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-1")).all()) == 2
