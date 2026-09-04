from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.services.customer_communications import CustomerCommunicationEmailHeaderSyncResult
from app.services.zoho_email_history_import import ZohoEmailHistoryImportService


class FakeCustomerCommunications:
    def __init__(self) -> None:
        self.customer_ids: list[int] = []

    def sync_customer_email_headers(self, *, customer_id: int) -> CustomerCommunicationEmailHeaderSyncResult:
        self.customer_ids.append(customer_id)
        return CustomerCommunicationEmailHeaderSyncResult(emails=customer_id)


def test_historical_email_import_processes_each_customer_once_and_completes():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first = Customer(name="First", zoho_id="zoho-first")
        second = Customer(name="Second", zoho_id="zoho-second")
        db.add_all([first, second])
        db.commit()

        communications = FakeCustomerCommunications()
        service = ZohoEmailHistoryImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            public_base_url="https://hub.example",
            communication_service=communications,  # type: ignore[arg-type]
        )
        status, started = service.start(requested_by="operator")

        assert started is True
        assert status.total_customers == 2
        assert service.process_next_customer() == "succeeded"

        # A fresh service instance simulates the worker resuming after a restart.
        resumed = ZohoEmailHistoryImportService(
            db=db,
            cipher=SecretCipher("a" * 32),
            public_base_url="https://hub.example",
            communication_service=communications,  # type: ignore[arg-type]
        )
        assert resumed.process_next_customer() == "succeeded"
        assert resumed.process_next_customer() == "completed"

        final = resumed.status()
        assert final is not None
        assert final.status == "completed"
        assert final.processed_customers == 2
        assert final.imported_emails == first.id + second.id
        assert communications.customer_ids == [first.id, second.id]
