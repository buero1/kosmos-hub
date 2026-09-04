"""Durable, rate-friendly batches for loading full Zoho email content."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer_communication import CustomerZohoEmail
from app.models.zoho_email_content_import import ZohoEmailContentImport, ZohoEmailContentImportItem
from app.services.customer_communications import CustomerCommunicationService
from app.services.zoho_crm import ZohoCrmError


_ACTIVE_STATUSES = ("pending", "running")


@dataclass(frozen=True)
class ZohoEmailContentImportStatus:
    id: int
    status: str
    requested_limit: int
    total_emails: int
    processed_emails: int
    loaded_emails: int
    failed_emails: int
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None


class ZohoEmailContentImportService:
    """Load selected message bodies without repeating headers that already have content."""

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

    def status(self) -> ZohoEmailContentImportStatus | None:
        latest = self.db.scalar(
            select(ZohoEmailContentImport).order_by(ZohoEmailContentImport.id.desc()).limit(1)
        )
        return self._status(latest) if latest is not None else None

    def start(self, *, requested_by: str, limit: int) -> tuple[ZohoEmailContentImportStatus, bool]:
        if limit < 1 or limit > 500:
            raise ValueError("Der E-Mail-Inhaltstest muss zwischen 1 und 500 Nachrichten umfassen.")
        active = self._active_import()
        if active is not None:
            return self._status(active), False

        latest = self.db.scalar(
            select(ZohoEmailContentImport).order_by(ZohoEmailContentImport.id.desc()).limit(1)
        )
        if latest is not None and latest.status == "completed":
            if latest.failed_emails:
                self._retry_failed_items(latest)
                return self._status(latest), True
            # This control deliberately runs one fixed test batch. A later full import
            # is a separate, explicit action rather than an accidental repeat click.
            return self._status(latest), False

        selected_email_ids: list[int] = []
        candidates = self.db.scalars(
            select(CustomerZohoEmail)
            .where(
                CustomerZohoEmail.zoho_message_id.is_not(None),
                CustomerZohoEmail.zoho_module.is_not(None),
                CustomerZohoEmail.zoho_record_id.is_not(None),
            )
            .order_by(CustomerZohoEmail.zoho_sent_at.desc(), CustomerZohoEmail.id.desc())
        )
        for email in candidates:
            if self.communications.has_loaded_email_content(email):
                continue
            selected_email_ids.append(email.id)
            if len(selected_email_ids) == limit:
                break

        if not selected_email_ids:
            raise ValueError("Für den Abruf gibt es keine Zoho-E-Mails ohne gespeicherten Inhalt.")

        run = ZohoEmailContentImport(
            requested_by=requested_by[:128],
            status="pending",
            requested_limit=limit,
            total_emails=len(selected_email_ids),
        )
        self.db.add(run)
        self.db.flush()
        self.db.add_all(
            ZohoEmailContentImportItem(email_import_id=run.id, email_id=email_id)
            for email_id in selected_email_ids
        )
        self.db.flush()
        return self._status(run), True

    def process_next_email(self) -> str | None:
        """Load the next selected email, recording failures without stopping the batch."""
        run = self._active_import()
        if run is None:
            return None
        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC)

        item = self.db.scalar(
            select(ZohoEmailContentImportItem)
            .where(
                ZohoEmailContentImportItem.email_import_id == run.id,
                ZohoEmailContentImportItem.status == "pending",
            )
            .order_by(ZohoEmailContentImportItem.id.asc())
            .limit(1)
        )
        if item is None:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            self.db.commit()
            return "completed"

        run_id = run.id
        item_id = item.id
        email_id = item.email_id
        # Mark the batch as running before the remote request so a restart resumes safely.
        self.db.commit()
        try:
            email = self.db.get(CustomerZohoEmail, email_id)
            if email is None:
                raise ValueError("Die ausgewählte E-Mail wurde im Hub nicht mehr gefunden.")
            self.communications.load_email_content(customer_id=email.customer_id, email_id=email.id)
        except (ValueError, ZohoCrmError) as exc:
            self.db.rollback()
            self._record_failure(run_id=run_id, item_id=item_id, email_id=email_id, error=str(exc))
            return "failed"
        except Exception as exc:
            self.db.rollback()
            self._record_failure(run_id=run_id, item_id=item_id, email_id=email_id, error=str(exc))
            return "failed"

        run = self.db.get(ZohoEmailContentImport, run_id)
        item = self.db.get(ZohoEmailContentImportItem, item_id)
        if run is None or item is None:
            self.db.rollback()
            return None
        item.status = "loaded"
        item.last_error = None
        run.processed_emails += 1
        run.loaded_emails += 1
        run.last_error = None
        self.db.commit()
        return "succeeded"

    def _record_failure(self, *, run_id: int, item_id: int, email_id: int, error: str) -> None:
        message = self._safe_error_message(error)
        run = self.db.get(ZohoEmailContentImport, run_id)
        item = self.db.get(ZohoEmailContentImportItem, item_id)
        email = self.db.get(CustomerZohoEmail, email_id)
        if run is None or item is None:
            self.db.rollback()
            return
        item.status = "failed"
        item.last_error = message
        if email is not None:
            email.last_error = message
        run.processed_emails += 1
        run.failed_emails += 1
        run.last_error = message
        self.db.commit()

    def _retry_failed_items(self, run: ZohoEmailContentImport) -> None:
        failed_items = self.db.scalars(
            select(ZohoEmailContentImportItem).where(
                ZohoEmailContentImportItem.email_import_id == run.id,
                ZohoEmailContentImportItem.status == "failed",
            )
        ).all()
        for item in failed_items:
            item.status = "pending"
            item.last_error = None
        run.status = "pending"
        run.processed_emails -= len(failed_items)
        run.failed_emails = 0
        run.completed_at = None
        run.last_error = None
        self.db.flush()

    @staticmethod
    def _safe_error_message(error: str) -> str:
        if "Data too long for column 'encrypted_payload_json'" in error:
            return "Der E-Mail-Inhalt überschreitet die bisherige Speichergröße im Hub."
        if "Zoho rejected the request" in error:
            return error.splitlines()[0][:1_000]
        return "Der E-Mail-Inhalt konnte nicht aus Zoho geladen werden."

    def _active_import(self) -> ZohoEmailContentImport | None:
        return self.db.scalar(
            select(ZohoEmailContentImport)
            .where(ZohoEmailContentImport.status.in_(_ACTIVE_STATUSES))
            .order_by(ZohoEmailContentImport.id.asc())
            .limit(1)
        )

    @staticmethod
    def _status(run: ZohoEmailContentImport) -> ZohoEmailContentImportStatus:
        return ZohoEmailContentImportStatus(
            id=run.id,
            status=run.status,
            requested_limit=run.requested_limit,
            total_emails=run.total_emails,
            processed_emails=run.processed_emails,
            loaded_emails=run.loaded_emails,
            failed_emails=run.failed_emails,
            started_at=run.started_at,
            completed_at=run.completed_at,
            last_error=run.last_error,
        )
