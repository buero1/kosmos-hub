"""Durable, rate-friendly batches for copying Zoho email attachments into Hub storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer_communication import CustomerEmailAttachment, CustomerZohoEmail
from app.models.zoho_email_attachment_import import ZohoEmailAttachmentImport, ZohoEmailAttachmentImportItem
from app.services.customer_communications import CustomerCommunicationService
from app.services.email_attachment_storage import EmailAttachmentStorageError
from app.services.zoho_crm import ZohoCrmError


_ACTIVE_STATUSES = ("pending", "running")


@dataclass(frozen=True)
class ZohoEmailAttachmentImportStatus:
    id: int
    status: str
    requested_limit: int
    total_attachments: int
    processed_attachments: int
    stored_attachments: int
    failed_attachments: int
    stored_bytes: int
    cancel_requested: bool
    consecutive_failures: int
    continue_automatically: bool
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None


class ZohoEmailAttachmentImportService:
    """Save Zoho attachment binaries once, keeping each batch restart-safe."""

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

    def status(self) -> ZohoEmailAttachmentImportStatus | None:
        latest = self.db.scalar(
            select(ZohoEmailAttachmentImport).order_by(ZohoEmailAttachmentImport.id.desc()).limit(1)
        )
        return self._status(latest) if latest is not None else None

    def start(
        self,
        *,
        requested_by: str,
        limit: int = 100,
        continue_automatically: bool = True,
        initial_consecutive_failures: int = 0,
    ) -> tuple[ZohoEmailAttachmentImportStatus, bool]:
        if limit < 1 or limit > 500:
            raise ValueError("Eine Anhangs-Charge muss zwischen 1 und 500 Dateien umfassen.")
        active = self._active_import()
        if active is not None:
            return self._status(active), False

        self.communications.ensure_email_attachment_storage()
        stored_attachment_keys = set(
            self.db.execute(
                select(CustomerEmailAttachment.email_id, CustomerEmailAttachment.source_attachment_id)
            ).all()
        )
        selected_attachments: list[tuple[int, str]] = []
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
            for attachment in self.communications.email_attachments_for_import(email):
                if (email.id, attachment.id) in stored_attachment_keys:
                    continue
                selected_attachments.append((email.id, attachment.id))
                if len(selected_attachments) == limit:
                    break
            if len(selected_attachments) == limit:
                break

        if not selected_attachments:
            raise ValueError("Für den Abruf gibt es keine Zoho-Anhänge ohne lokale Sicherung.")

        run = ZohoEmailAttachmentImport(
            requested_by=requested_by[:128],
            status="pending",
            continue_automatically=continue_automatically,
            consecutive_failures=initial_consecutive_failures,
            requested_limit=limit,
            total_attachments=len(selected_attachments),
        )
        self.db.add(run)
        self.db.flush()
        self.db.add_all(
            ZohoEmailAttachmentImportItem(
                attachment_import_id=run.id,
                email_id=email_id,
                source_attachment_id=attachment_id,
            )
            for email_id, attachment_id in selected_attachments
        )
        self.db.flush()
        return self._status(run), True

    def cancel(self) -> tuple[ZohoEmailAttachmentImportStatus | None, bool]:
        run = self._active_import()
        if run is None:
            return self.status(), False
        run.cancel_requested = True
        self.db.flush()
        return self._status(run), True

    def process_next_attachment(self) -> str | None:
        """Download one pending attachment and keep retry evidence when it fails."""
        run = self._active_import()
        if run is None:
            return None
        if run.cancel_requested:
            run.status = "cancelled"
            run.completed_at = datetime.now(UTC)
            run.last_error = "Der Import wurde durch den Benutzer abgebrochen."
            self.db.commit()
            return "cancelled"
        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC)

        item = self.db.scalar(
            select(ZohoEmailAttachmentImportItem)
            .where(
                ZohoEmailAttachmentImportItem.attachment_import_id == run.id,
                ZohoEmailAttachmentImportItem.status == "pending",
            )
            .order_by(ZohoEmailAttachmentImportItem.id.asc())
            .limit(1)
        )
        if item is None:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            self.db.commit()
            if run.continue_automatically:
                try:
                    _, started = self.start(
                        requested_by=run.requested_by,
                        limit=run.requested_limit,
                        continue_automatically=True,
                        initial_consecutive_failures=run.consecutive_failures,
                    )
                except EmailAttachmentStorageError as exc:
                    run = self.db.get(ZohoEmailAttachmentImport, run.id)
                    if run is not None:
                        run.status = "stopped"
                        run.completed_at = datetime.now(UTC)
                        run.last_error = str(exc)
                        self.db.commit()
                    return "stopped"
                except ValueError:
                    return "completed"
                if started:
                    self.db.commit()
                    return "continued"
            return "completed"

        run_id = run.id
        item_id = item.id
        email_id = item.email_id
        attachment_id = item.source_attachment_id
        # Commit before the remote request so a restart resumes the same pending item safely.
        self.db.commit()
        try:
            email = self.db.get(CustomerZohoEmail, email_id)
            if email is None:
                raise ValueError("Die ausgewählte E-Mail wurde im Hub nicht mehr gefunden.")
            stored = self.communications.store_email_attachment(
                customer_id=email.customer_id,
                email_id=email.id,
                attachment_id=attachment_id,
            )
        except (EmailAttachmentStorageError, ValueError, ZohoCrmError) as exc:
            self.db.rollback()
            stopped = self._record_failure(run_id=run_id, item_id=item_id, error=str(exc))
            return "stopped" if stopped else "failed"
        except Exception as exc:
            self.db.rollback()
            stopped = self._record_failure(run_id=run_id, item_id=item_id, error=str(exc))
            return "stopped" if stopped else "failed"

        run = self.db.get(ZohoEmailAttachmentImport, run_id)
        item = self.db.get(ZohoEmailAttachmentImportItem, item_id)
        if run is None or item is None:
            self.db.rollback()
            return None
        item.status = "stored"
        item.last_error = None
        run.processed_attachments += 1
        run.stored_attachments += 1
        run.stored_bytes += stored.byte_size
        run.consecutive_failures = 0
        run.last_error = None
        self.db.commit()
        return "succeeded"

    def _record_failure(self, *, run_id: int, item_id: int, error: str) -> bool:
        message = self._safe_error_message(error)
        run = self.db.get(ZohoEmailAttachmentImport, run_id)
        item = self.db.get(ZohoEmailAttachmentImportItem, item_id)
        if run is None or item is None:
            self.db.rollback()
            return False
        item.status = "failed"
        item.last_error = message
        run.processed_attachments += 1
        run.failed_attachments += 1
        run.consecutive_failures += 1
        stopped = run.consecutive_failures >= 3
        if stopped:
            run.status = "stopped"
            run.completed_at = datetime.now(UTC)
            run.last_error = "Import nach drei aufeinanderfolgenden Fehlern automatisch angehalten."
        else:
            run.last_error = message
        self.db.commit()
        return stopped

    def _active_import(self) -> ZohoEmailAttachmentImport | None:
        return self.db.scalar(
            select(ZohoEmailAttachmentImport)
            .where(ZohoEmailAttachmentImport.status.in_(_ACTIVE_STATUSES))
            .order_by(ZohoEmailAttachmentImport.id.asc())
            .limit(1)
        )

    @staticmethod
    def _safe_error_message(error: str) -> str:
        if "Zoho rejected the request" in error:
            return error.splitlines()[0][:1_000]
        if "nicht rechtzeitig geantwortet" in error:
            return error
        if "nicht genug freier Speicherplatz" in error:
            return error
        return "Der Anhang konnte nicht aus Zoho gesichert werden."

    @staticmethod
    def _status(run: ZohoEmailAttachmentImport) -> ZohoEmailAttachmentImportStatus:
        return ZohoEmailAttachmentImportStatus(
            id=run.id,
            status=run.status,
            requested_limit=run.requested_limit,
            total_attachments=run.total_attachments,
            processed_attachments=run.processed_attachments,
            stored_attachments=run.stored_attachments,
            failed_attachments=run.failed_attachments,
            stored_bytes=run.stored_bytes,
            cancel_requested=run.cancel_requested,
            consecutive_failures=run.consecutive_failures,
            continue_automatically=run.continue_automatically,
            started_at=run.started_at,
            completed_at=run.completed_at,
            last_error=run.last_error,
        )
