from datetime import UTC, datetime, timedelta

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
        self.failing_email_id = failing_email_id
        self.loaded_email_ids: list[int] = []

    @staticmethod
    def has_loaded_email_content(_: CustomerZohoEmail) -> bool:
        return False

    def load_email_content(self, *, customer_id: int, email_id: int):
        del customer_id
        if email_id == self.failing_email_id:
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
        status, started = service.start(requested_by="operator", limit=3)

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
        assert communications.loaded_email_ids == [emails[0].id, emails[2].id]
        assert db.scalars(
            select(ZohoEmailContentImportItem.status).order_by(ZohoEmailContentImportItem.id.asc())
        ).all() == ["loaded", "failed", "loaded"]

        communications.failing_email_id = 0
        retry_status, retried = service.start(requested_by="operator", limit=3)

        assert retried is True
        assert retry_status.status == "pending"
        assert retry_status.processed_emails == 2
        assert retry_status.failed_emails == 0
        assert service.process_next_email() == "succeeded"
        assert service.process_next_email() == "completed"
        assert service.status().loaded_emails == 3  # type: ignore[union-attr]

        duplicate_status, duplicate_started = service.start(requested_by="operator", limit=3)

        assert duplicate_started is False
        assert duplicate_status.id == final.id
        assert duplicate_status.loaded_emails == 3
