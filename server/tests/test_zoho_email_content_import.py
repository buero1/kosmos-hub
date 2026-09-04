from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.zoho_email_content_import import ZohoEmailContentImportItem
from app.services.zoho_email_content_import import ZohoEmailContentImportService


class FakeCustomerCommunications:
    def __init__(self, *, failing_email_id: int) -> None:
        self.failing_email_ids = {failing_email_id} if failing_email_id else set()
        self.loaded_email_ids: list[int] = []

    def has_loaded_email_content(self, email: CustomerZohoEmail) -> bool:
        return email.id in self.loaded_email_ids

    def load_email_content(self, *, customer_id: int, email_id: int):
        del customer_id
        if email_id in self.failing_email_ids:
            raise ValueError("Zoho content is unavailable for this message.")
        self.loaded_email_ids.append(email_id)


def test_email_content_import_continues_after_one_email_fails():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Customer", zoho_id="zoho-customer")
        db.add(customer)
        db.flush()
        now = datetime.now(UTC)
        emails = [
            CustomerZohoEmail(
                customer_id=customer.id,
                zoho_message_id=f"message-{index}",
                zoho_module="Accounts",
                zoho_record_id="zoho-customer",
                source="zoho",
                encrypted_payload_json="header",
                zoho_sent_at=now - timedelta(minutes=index),
            )
            for index in range(3)
        ]
        db.add_all(emails)
        db.commit()

        communications = FakeCustomerCommunications(failing_email_id=emails[1].id)
        service = ZohoEmailContentImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            public_base_url="https://hub.example",
            communication_service=communications,  # type: ignore[arg-type]
        )
        status, started = service.start(requested_by="operator", limit=3, continue_automatically=False)

        assert started is True
        assert status.total_emails == 3
        assert service.process_next_email() == "succeeded"
        assert service.process_next_email() == "failed"
        assert service.process_next_email() == "succeeded"
        assert service.process_next_email() == "completed"

        final = service.status()
        assert final is not None
        assert final.status == "completed"
        assert final.processed_emails == 3
        assert final.loaded_emails == 2
        assert final.failed_emails == 1
        assert final.consecutive_failures == 0
        assert communications.loaded_email_ids == [emails[0].id, emails[2].id]
        assert db.scalars(
            select(ZohoEmailContentImportItem.status).order_by(ZohoEmailContentImportItem.id.asc())
        ).all() == ["loaded", "failed", "loaded"]

        communications.failing_email_ids.clear()
        retry_status, retried = service.start(requested_by="operator", limit=3, continue_automatically=False)

        assert retried is True
        assert retry_status.status == "pending"
        assert retry_status.id != final.id
        assert retry_status.total_emails == 1
        assert service.process_next_email() == "succeeded"
        assert service.process_next_email() == "completed"
        assert service.status().loaded_emails == 1  # type: ignore[union-attr]

        with pytest.raises(ValueError, match="keine Zoho-E-Mails ohne gespeicherten Inhalt"):
            service.start(requested_by="operator", limit=3, continue_automatically=False)


def test_email_content_import_stops_after_three_consecutive_failures():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Customer", zoho_id="zoho-customer")
        db.add(customer)
        db.flush()
        emails = [
            CustomerZohoEmail(
                customer_id=customer.id,
                zoho_message_id=f"message-{index}",
                zoho_module="Accounts",
                zoho_record_id="zoho-customer",
                source="zoho",
                encrypted_payload_json="header",
            )
            for index in range(4)
        ]
        db.add_all(emails)
        db.commit()

        communications = FakeCustomerCommunications(failing_email_id=emails[0].id)
        communications.failing_email_ids = {email.id for email in emails}
        service = ZohoEmailContentImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            public_base_url="https://hub.example",
            communication_service=communications,  # type: ignore[arg-type]
        )
        service.start(requested_by="operator", limit=4, continue_automatically=False)

        assert service.process_next_email() == "failed"
        assert service.process_next_email() == "failed"
        assert service.process_next_email() == "stopped"
        final = service.status()
        assert final is not None
        assert final.status == "stopped"
        assert final.processed_emails == 3
        assert final.failed_emails == 3
        assert final.consecutive_failures == 3


def test_email_content_import_can_be_cancelled_before_the_next_email():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Customer", zoho_id="zoho-customer")
        db.add(customer)
        db.flush()
        email = CustomerZohoEmail(
            customer_id=customer.id,
            zoho_message_id="message-1",
            zoho_module="Accounts",
            zoho_record_id="zoho-customer",
            source="zoho",
            encrypted_payload_json="header",
        )
        db.add(email)
        db.commit()

        service = ZohoEmailContentImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            public_base_url="https://hub.example",
            communication_service=FakeCustomerCommunications(failing_email_id=0),  # type: ignore[arg-type]
        )
        service.start(requested_by="operator", limit=1, continue_automatically=False)
        status, requested = service.cancel()

        assert requested is True
        assert status is not None
        assert status.cancel_requested is True
        assert service.process_next_email() == "cancelled"
        assert service.status().status == "cancelled"  # type: ignore[union-attr]


def test_email_content_import_continues_with_a_follow_up_batch():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Customer", zoho_id="zoho-customer")
        db.add(customer)
        db.flush()
        emails = [
            CustomerZohoEmail(
                customer_id=customer.id,
                zoho_message_id=f"message-{index}",
                zoho_module="Accounts",
                zoho_record_id="zoho-customer",
                source="zoho",
                encrypted_payload_json="header",
            )
            for index in range(2)
        ]
        db.add_all(emails)
        db.commit()

        communications = FakeCustomerCommunications(failing_email_id=0)
        service = ZohoEmailContentImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            public_base_url="https://hub.example",
            communication_service=communications,  # type: ignore[arg-type]
        )
        first, started = service.start(requested_by="operator", limit=1, continue_automatically=True)

        assert started is True
        assert service.process_next_email() == "succeeded"
        assert service.process_next_email() == "continued"
        second = service.status()
        assert second is not None
        assert second.id != first.id
        assert second.continue_automatically is True
        assert second.total_emails == 1
        assert service.process_next_email() == "succeeded"
        assert service.process_next_email() == "completed"
