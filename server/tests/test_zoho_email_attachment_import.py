import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.zoho_email_attachment_import import ZohoEmailAttachmentImportItem
from app.services.zoho_email_attachment_import import ZohoEmailAttachmentImportService


class FakeCustomerCommunications:
    def __init__(self, *, failing_attachment_id: str = "") -> None:
        self.failing_attachment_id = failing_attachment_id
        self.stored_attachment_ids: set[tuple[int, str]] = set()
        self.store_calls: list[tuple[int, int, str]] = []

    def ensure_email_attachment_storage(self) -> None:
        return None

    def email_attachments_for_import(self, email: CustomerZohoEmail):
        payload = json.loads(email.encrypted_payload_json)
        return tuple(
            type("Attachment", (), {"id": item["id"]})()
            for item in payload.get("attachments", [])
        )

    def store_email_attachment(self, *, customer_id: int, email_id: int, attachment_id: str):
        self.store_calls.append((customer_id, email_id, attachment_id))
        if attachment_id == self.failing_attachment_id:
            raise ValueError("Zoho attachment is unavailable.")
        self.stored_attachment_ids.add((email_id, attachment_id))
        return type("StoredAttachment", (), {"byte_size": 42})()


def _email(*, customer_id: int, attachment_ids: list[str]) -> CustomerZohoEmail:
    return CustomerZohoEmail(
        customer_id=customer_id,
        zoho_message_id="message-1",
        zoho_module="Accounts",
        zoho_record_id="account-1",
        source="zoho",
        encrypted_payload_json=json.dumps(
            {"attachments": [{"id": attachment_id, "name": f"{attachment_id}.pdf"} for attachment_id in attachment_ids]}
        ),
    )


def test_attachment_import_stores_each_attachment_and_records_a_failure():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Customer", zoho_id="account-1")
        db.add(customer)
        db.flush()
        email = _email(customer_id=customer.id, attachment_ids=["attachment-1", "attachment-2"])
        db.add(email)
        db.commit()

        communications = FakeCustomerCommunications(failing_attachment_id="attachment-2")
        service = ZohoEmailAttachmentImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            public_base_url="https://hub.example",
            communication_service=communications,  # type: ignore[arg-type]
        )
        status, started = service.start(requested_by="operator", limit=100, continue_automatically=False)

        assert started is True
        assert status.total_attachments == 2
        assert service.process_next_attachment() == "succeeded"
        assert service.process_next_attachment() == "failed"
        assert service.process_next_attachment() == "completed"

        final = service.status()
        assert final is not None
        assert final.status == "completed"
        assert final.stored_attachments == 1
        assert final.failed_attachments == 1
        assert final.stored_bytes == 42
        assert db.scalars(
            select(ZohoEmailAttachmentImportItem.status).order_by(ZohoEmailAttachmentImportItem.id.asc())
        ).all() == ["stored", "failed"]
