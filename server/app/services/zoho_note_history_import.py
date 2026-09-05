"""Durable, rate-friendly initial import for Zoho Account notes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.zoho_note_history_import import ZohoNoteHistoryImport
from app.services.customer_communications import CustomerCommunicationService
from app.services.zoho_crm import ZohoCrmError


_ACTIVE_STATUSES = ("pending", "running")


@dataclass(frozen=True)
class ZohoNoteHistoryImportStatus:
    id: int
    status: str
    total_customers: int
    processed_customers: int
    imported_notes: int
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None


class ZohoNoteHistoryImportService:
    """Import one customer's Account notes at a time so a restart can resume safely."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        public_base_url: str,
        communication_service: CustomerCommunicationService | None = None,
    ) -> None:
        self.db = db
        self.communications = communication_service or CustomerCommunicationService(
            db=db,
            cipher=cipher,
            public_base_url=public_base_url,
        )

    def status(self) -> ZohoNoteHistoryImportStatus | None:
        latest = self.db.scalar(
            select(ZohoNoteHistoryImport).order_by(ZohoNoteHistoryImport.id.desc()).limit(1)
        )
        return self._status(latest) if latest is not None else None

    def start(self, *, requested_by: str) -> tuple[ZohoNoteHistoryImportStatus, bool]:
        active = self._active_import()
        if active is not None:
            return self._status(active), False

        total_customers = int(
            self.db.scalar(
                select(func.count()).select_from(Customer).where(Customer.zoho_id.is_not(None))
            )
            or 0
        )
        if total_customers == 0:
            raise ValueError("Es gibt keine mit Zoho verknüpften Kunden für den Notizimport.")

        run = ZohoNoteHistoryImport(
            requested_by=requested_by[:128],
            status="pending",
            total_customers=total_customers,
        )
        self.db.add(run)
        self.db.flush()
        return self._status(run), True

    def process_next_customer(self) -> str | None:
        """Import the next customer's notes, or finish the active run."""
        run = self._active_import()
        if run is None:
            return None

        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC)
        customer = self.db.scalar(
            select(Customer)
            .where(
                Customer.zoho_id.is_not(None),
                Customer.id > (run.last_customer_id or 0),
            )
            .order_by(Customer.id.asc())
            .limit(1)
        )
        if customer is None:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            run.last_error = None
            self.db.commit()
            return "completed"

        run_id = run.id
        customer_id = customer.id
        # Persist the running state before calling Zoho, so a restart retries this customer.
        self.db.commit()
        try:
            note_count = self.communications.sync_customer_notes(customer_id=customer_id)
        except (ValueError, ZohoCrmError) as exc:
            self.db.rollback()
            self._fail(run_id=run_id, error=str(exc))
            return "failed"
        except Exception as exc:
            self.db.rollback()
            self._fail(run_id=run_id, error=str(exc))
            return "failed"

        run = self.db.get(ZohoNoteHistoryImport, run_id)
        if run is None:
            self.db.rollback()
            return None
        run.last_customer_id = customer_id
        run.processed_customers += 1
        run.imported_notes += note_count
        run.last_error = None
        self.db.commit()
        return "succeeded"

    def _active_import(self) -> ZohoNoteHistoryImport | None:
        return self.db.scalar(
            select(ZohoNoteHistoryImport)
            .where(ZohoNoteHistoryImport.status.in_(_ACTIVE_STATUSES))
            .order_by(ZohoNoteHistoryImport.id.asc())
            .limit(1)
        )

    def _fail(self, *, run_id: int, error: str) -> None:
        run = self.db.get(ZohoNoteHistoryImport, run_id)
        if run is None:
            return
        run.status = "failed"
        run.completed_at = datetime.now(UTC)
        run.last_error = error[:1_000]
        self.db.commit()

    @staticmethod
    def _status(run: ZohoNoteHistoryImport) -> ZohoNoteHistoryImportStatus:
        return ZohoNoteHistoryImportStatus(
            id=run.id,
            status=run.status,
            total_customers=run.total_customers,
            processed_customers=run.processed_customers,
            imported_notes=run.imported_notes,
            started_at=run.started_at,
            completed_at=run.completed_at,
            last_error=run.last_error,
        )
