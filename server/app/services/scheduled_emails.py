"""Durable, encrypted scheduling and delivery for user-authored emails."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from secrets import token_hex
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_scheduled_email import HubScheduledEmail, HubScheduledEmailAttachment
from app.services.customer_communications import (
    CustomerCommunicationAttachment,
    CustomerCommunicationAttachmentDownload,
    CustomerCommunicationAttachmentUpload,
    CustomerCommunicationService,
)
from app.services.email_attachment_storage import EmailAttachmentStorage, EmailAttachmentStorageError, track_attachment_file
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_transport import HubMailboxTransportService


_PENDING_STATUSES = ("scheduled", "retrying")
_VISIBLE_STATUSES = (*_PENDING_STATUSES, "sending", "failed")
_RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60))
_STALE_SENDING_AFTER = timedelta(minutes=15)
_BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class ScheduledEmailProcessResult:
    sent: int = 0
    retried: int = 0
    failed: int = 0


class ScheduledEmailService:
    """Store scheduled messages and deliver each due message through the normal mail services."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        public_base_url: str = "",
        attachment_storage: EmailAttachmentStorage | None = None,
    ) -> None:
        self.db = db
        self.cipher = cipher
        self.public_base_url = public_base_url.rstrip("/")
        settings = get_settings()
        self.attachment_storage = attachment_storage or EmailAttachmentStorage(
            root=settings.email_attachment_storage_dir,
            cipher=cipher,
            min_free_bytes=settings.email_attachment_import_min_free_bytes,
        )

    @staticmethod
    def parse_berlin_datetime(value: str) -> datetime:
        """Convert a browser datetime-local value to the naive UTC format used by the database."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("Wähle Datum und Uhrzeit für den geplanten Versand aus.")
        try:
            local_value = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("Datum oder Uhrzeit für den geplanten Versand ist ungültig.") from exc
        if local_value.tzinfo is None:
            local_value = local_value.replace(tzinfo=_BERLIN)
        return local_value.astimezone(UTC).replace(tzinfo=None)

    def schedule(
        self,
        *,
        actor: str,
        scheduled_at: datetime,
        sender_email: str,
        recipient_email: str,
        recipient_name: str,
        subject: str,
        content: str,
        cc_emails: str,
        customer_id: int | None = None,
        lead_id: int | None = None,
        dunning_id: int | None = None,
        recipient_key: str = "",
        template_id: str = "",
        reply_to_email_id: int | None = None,
        forward_from_email_id: int | None = None,
        attachments: tuple[CustomerCommunicationAttachmentUpload, ...] = (),
        scheduled_email_id: int | None = None,
    ) -> HubScheduledEmail:
        from app.services.hub_mailbox_permissions import MailboxPermissions
        from app.services.hub_mailbox_access import HubMailboxAccess
        MailboxPermissions(db=self.db, actor=actor).require_sender(sender_email, "send")
        if scheduled_email_id is not None:
            HubMailboxAccess(db=self.db, cipher=self.cipher, actor=actor).require(f"scheduled-{scheduled_email_id}", "edit")
        now = self._utc_now()
        from app.services.hub_deletion import lock_parent
        if customer_id is not None and lead_id is not None:
            raise ValueError("Bitte nur einen Kunden oder Lead verknuepfen.")
        if lead_id is not None:
            lock_parent(self.db, kind="leads", record_id=lead_id)
        if customer_id is not None:
            lock_parent(self.db, kind="customers", record_id=customer_id)
        normalized_scheduled_at = self._naive_utc(scheduled_at)
        if normalized_scheduled_at <= now:
            raise ValueError("Der geplante Versandzeitpunkt muss in der Zukunft liegen.")
        if reply_to_email_id is not None and forward_from_email_id is not None:
            raise ValueError("Eine E-Mail kann nicht gleichzeitig Antwort und Weiterleitung sein.")

        communications = CustomerCommunicationService(
            db=self.db,
            cipher=self.cipher,
            public_base_url=self.public_base_url,
            attachment_storage=self.attachment_storage,
        )
        normalized_subject = communications._required_text(subject, "Betreff", maximum=500)
        normalized_content = communications._sanitized_email_content(content)
        normalized_sender = sender_email.strip().casefold()
        normalized_recipient_name = recipient_name.strip()[:255]
        normalized_recipient_key = recipient_key.strip()[:255]

        if customer_id is not None:
            customer = self.db.get(Customer, customer_id)
            if customer is None:
                raise ValueError("Der verknüpfte Kunde wurde nicht gefunden.")
            sender = next(
                (item for item in communications.list_senders() if item.email.casefold() == normalized_sender),
                None,
            )
            if sender is None:
                raise ValueError("Wähle eine aktuell eingerichtete Absenderadresse aus.")
            recipient = next(
                (
                    item
                    for item in communications._recipients_for_customer(
                        customer,
                        include_account_email=not normalized_recipient_key.startswith("contact:"),
                    )
                    if item.key == normalized_recipient_key
                ),
                None,
            )
            if recipient is None or recipient.email.casefold() != recipient_email.strip().casefold():
                raise ValueError("Wähle eine aktuelle E-Mail-Adresse dieses Kunden oder Kontakts aus.")
            normalized_recipient_email = recipient.email
            normalized_recipient_name = recipient.name
        else:
            transport = HubMailboxTransportService(db=self.db, cipher=self.cipher)
            sender = next(
                (item for item in transport.list_senders() if item.email.casefold() == normalized_sender),
                None,
            )
            if sender is None:
                raise ValueError("Wähle ein eingerichtetes Mittwald-Postfach als Absender aus.")
            parsed_name, parsed_email = parseaddr(recipient_email.strip())
            normalized_recipient_email = parsed_email.strip().casefold()
            if not normalized_recipient_email or "@" not in normalized_recipient_email:
                raise ValueError("Gib eine gültige Empfängeradresse ein.")
            normalized_recipient_name = normalized_recipient_name or parsed_name.strip() or normalized_recipient_email

        normalized_attachments = HubMailboxService._validated_direct_attachments(attachments)
        payload = {
            "recipient_lead_id": lead_id,
            "sender_email": sender.email,
            "recipient_email": normalized_recipient_email,
            "recipient_name": normalized_recipient_name,
            "recipient_key": normalized_recipient_key,
            "subject": normalized_subject,
            "content": normalized_content,
            "cc_emails": cc_emails.strip()[:2_000],
            "template_id": template_id.strip()[:255],
            "reply_to_email_id": reply_to_email_id,
            "forward_from_email_id": forward_from_email_id,
        }
        if scheduled_email_id is None:
            scheduled = HubScheduledEmail(
                lead_id=lead_id,
                customer_id=customer_id,
                dunning_id=dunning_id,
                creator_username=actor[:64],
                encrypted_payload_json="",
                scheduled_at=normalized_scheduled_at,
                next_attempt_at=normalized_scheduled_at,
                status="scheduled",
                message_id=f"<hub-scheduled-{token_hex(20)}@kosmos-medien.de>",
            )
            self.db.add(scheduled)
        else:
            scheduled = self._editable(scheduled_email_id=scheduled_email_id)
            if len(scheduled.attachments) + len(normalized_attachments) > 20:
                raise ValueError("Es können höchstens 20 Anhänge pro E-Mail versendet werden.")
            existing_attachment_bytes = sum(attachment.byte_size for attachment in scheduled.attachments)
            new_attachment_bytes = sum(len(attachment.content) for attachment in normalized_attachments)
            if existing_attachment_bytes + new_attachment_bytes > 50 * 1024 * 1024:
                raise ValueError("Die Anhänge sind zusammen größer als 50 MB.")
            scheduled.customer_id = customer_id
            scheduled.lead_id = lead_id
            if dunning_id is not None:
                scheduled.dunning_id = dunning_id
            scheduled.creator_username = actor[:64]
            scheduled.customer_email_id = None
            scheduled.mailbox_email_id = None
            scheduled.attempt_count = 0
            scheduled.last_error = None
            scheduled.locked_at = None
            scheduled.sent_at = None
        scheduled.encrypted_payload_json = self.cipher.encrypt(json.dumps(payload, ensure_ascii=False))
        scheduled.scheduled_at = normalized_scheduled_at
        scheduled.next_attempt_at = normalized_scheduled_at
        scheduled.status = "scheduled"
        self.db.flush()
        from app.services.hub_mailbox_permissions import bind_message
        bind_message(self.db, self.cipher, scheduled, replace=True)

        stored_keys: list[str] = []
        try:
            for attachment in normalized_attachments:
                storage_key = self.attachment_storage.store(attachment.content)
                stored_keys.append(storage_key)
                self.db.add(
                    HubScheduledEmailAttachment(
                        scheduled_email_id=scheduled.id,
                        filename=attachment.filename.strip()[:255],
                        storage_key=storage_key,
                        content_type=(attachment.content_type or "application/octet-stream")[:128],
                        byte_size=len(attachment.content),
                        stored_at=now,
                    )
                )
            self.db.flush()
        except (EmailAttachmentStorageError, ValueError) as exc:
            for storage_key in stored_keys:
                self.attachment_storage.remove(storage_key)
            self.db.delete(scheduled)
            self.db.flush()
            raise ValueError(str(exc)) from exc
        return scheduled

    def get_compose_context(self, *, scheduled_email_id: int) -> dict[str, object]:
        scheduled = self._editable(scheduled_email_id=scheduled_email_id)
        payload = self.payload(scheduled)
        customer = self.db.get(Customer, scheduled.customer_id) if scheduled.customer_id is not None else None
        recipient_email = self._text(payload.get("recipient_email"))
        recipient_name = self._text(payload.get("recipient_name")) or recipient_email
        recipient_key = self._text(payload.get("recipient_key"))
        local_scheduled_at = scheduled.scheduled_at.replace(tzinfo=UTC).astimezone(_BERLIN)
        return {
            "action": "scheduled",
            "scheduled_email_id": scheduled.id,
            "dunning_id": scheduled.dunning_id,
            "customer_id": customer.id if customer is not None else None,
            "lead_id": scheduled.lead_id,
            "recipient": (
                {"key": recipient_key, "name": recipient_name, "email": recipient_email}
                if customer is not None and recipient_key
                else None
            ),
            "recipient_email": recipient_email,
            "sender_email": self._text(payload.get("sender_email")),
            "subject": self._text(payload.get("subject")),
            "content": self._text(payload.get("content")),
            "cc_emails": self._text(payload.get("cc_emails")),
            "template_id": self._text(payload.get("template_id")),
            "reply_to_email_id": self._optional_int(payload.get("reply_to_email_id")),
            "forward_from_email_id": self._optional_int(payload.get("forward_from_email_id")),
            "scheduled_at": local_scheduled_at.strftime("%Y-%m-%dT%H:%M"),
            "attachments": [
                {"id": attachment.id, "filename": attachment.filename}
                for attachment in scheduled.attachments
            ],
        }

    def download_attachment(
        self,
        *,
        scheduled_email_id: int,
        attachment_id: int,
    ) -> CustomerCommunicationAttachmentDownload:
        scheduled = self._editable(scheduled_email_id=scheduled_email_id)
        attachment = next((item for item in scheduled.attachments if item.id == attachment_id), None)
        if attachment is None:
            raise ValueError("Der angeforderte Anhang gehört nicht zu dieser geplanten E-Mail.")
        try:
            content = self.attachment_storage.load(attachment.storage_key)
        except EmailAttachmentStorageError as exc:
            raise ValueError("Der gespeicherte Anhang ist nicht lesbar.") from exc
        return CustomerCommunicationAttachmentDownload(
            content=content,
            content_type=attachment.content_type,
            filename=attachment.filename,
        )

    def send_now(self, *, scheduled_email_id: int) -> ScheduledEmailProcessResult:
        scheduled = self._editable(scheduled_email_id=scheduled_email_id, action="send")
        scheduled.status = "scheduled"
        scheduled.next_attempt_at = self._utc_now()
        scheduled.locked_at = None
        scheduled.attempt_count = 0
        scheduled.last_error = None
        self.db.commit()
        return self._deliver(scheduled_id=scheduled.id)

    def cancel(self, *, scheduled_email_id: int) -> HubScheduledEmail:
        self.db.scalar(select(HubScheduledEmail).where(HubScheduledEmail.id == scheduled_email_id)
                       .with_for_update().execution_options(populate_existing=True))
        scheduled = self._editable(scheduled_email_id=scheduled_email_id, action="edit")
        storage_keys = [attachment.storage_key for attachment in scheduled.attachments]
        scheduled.attachments.clear()
        scheduled.status = "cancelled"
        scheduled.locked_at = None
        self.db.flush()
        for storage_key in storage_keys:
            track_attachment_file(self.db, self.attachment_storage, storage_key, removed=True)
        return scheduled

    def next_due_at(self) -> datetime | None:
        return self.db.scalar(
            select(func.min(HubScheduledEmail.next_attempt_at)).where(
                HubScheduledEmail.status.in_(_PENDING_STATUSES)
            )
        )

    def visible_for_customer(self, *, customer_id: int) -> tuple[HubScheduledEmail, ...]:
        return tuple(
            self.db.scalars(
                select(HubScheduledEmail)
                .where(HubScheduledEmail.customer_id == customer_id)
                .where(HubScheduledEmail.status.in_(_VISIBLE_STATUSES))
                .order_by(HubScheduledEmail.scheduled_at.desc(), HubScheduledEmail.id.desc())
            ).all()
        )

    def payload(self, scheduled: HubScheduledEmail) -> dict[str, object]:
        try:
            decoded = json.loads(self.cipher.decrypt(scheduled.encrypted_payload_json))
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def attachment_views(self, scheduled: HubScheduledEmail) -> tuple[CustomerCommunicationAttachment, ...]:
        return tuple(
            CustomerCommunicationAttachment(id=str(attachment.id), filename=attachment.filename)
            for attachment in scheduled.attachments
        )

    def process_due(self, *, limit: int = 25) -> ScheduledEmailProcessResult:
        now = self._utc_now()
        recovered_retries, recovered_failures = self._recover_stalled(now=now)
        due_ids = list(
            self.db.scalars(
                select(HubScheduledEmail.id)
                .where(HubScheduledEmail.status.in_(_PENDING_STATUSES))
                .where(HubScheduledEmail.next_attempt_at <= now)
                .order_by(HubScheduledEmail.next_attempt_at.asc(), HubScheduledEmail.id.asc())
                .limit(limit)
            )
        )
        sent = 0
        retried = recovered_retries
        failed = recovered_failures
        for scheduled_id in due_ids:
            outcome = self._deliver(scheduled_id=scheduled_id)
            sent += outcome.sent
            retried += outcome.retried
            failed += outcome.failed
        return ScheduledEmailProcessResult(sent=sent, retried=retried, failed=failed)

    def _deliver(self, *, scheduled_id: int) -> ScheduledEmailProcessResult:
        now = self._utc_now()
        scheduled = self.db.get(HubScheduledEmail, scheduled_id)
        if scheduled is None or scheduled.status not in _PENDING_STATUSES or scheduled.next_attempt_at > now:
            return ScheduledEmailProcessResult()
        scheduled.status = "sending"
        scheduled.locked_at = now
        self.db.commit()

        try:
            payload = self.payload(scheduled)
            from app.services.hub_mailbox_access import HubMailboxAccess
            scope = HubMailboxAccess(db=self.db, cipher=self.cipher, actor=scheduled.creator_username)
            scope.require(f"scheduled-{scheduled.id}", "send")
            scope.mailboxes.require_sender(self._text(payload.get("sender_email")), "send")
            attachments = tuple(
                CustomerCommunicationAttachmentUpload(
                    filename=attachment.filename,
                    content=self.attachment_storage.load(attachment.storage_key),
                    content_type=attachment.content_type,
                )
                for attachment in scheduled.attachments
            )
            reply_to_email_id = self._optional_int(payload.get("reply_to_email_id"))
            forward_from_email_id = self._optional_int(payload.get("forward_from_email_id"))
            if scheduled.customer_id is not None:
                result = CustomerCommunicationService(
                    db=self.db,
                    cipher=self.cipher,
                    public_base_url=self.public_base_url,
                    attachment_storage=self.attachment_storage,
                ).send_email(
                    customer_id=scheduled.customer_id,
                    actor=scheduled.creator_username,
                    sender_email=self._text(payload.get("sender_email")),
                    recipient_key=self._text(payload.get("recipient_key")),
                    subject=self._text(payload.get("subject")),
                    content=self._text(payload.get("content")),
                    template_id=self._text(payload.get("template_id")),
                    reply_to_email_id=reply_to_email_id,
                    cc_emails=self._text(payload.get("cc_emails")),
                    forward_from_email_id=forward_from_email_id,
                    attachments=attachments,
                    message_id=scheduled.message_id,
                    dunning_id=scheduled.dunning_id,
                )
                if not result.success:
                    error = result.message
                    self._remove_failed_customer_email(result.email_id)
                    return self._record_failure(scheduled_id=scheduled_id, error=error)
                scheduled.customer_email_id = result.email_id
            else:
                sent_email = HubMailboxService(
                    db=self.db,
                    cipher=self.cipher,
                    public_base_url=self.public_base_url,
                    attachment_storage=self.attachment_storage,
                    actor=scheduled.creator_username,
                ).send_direct_email(
                    sender_email=self._text(payload.get("sender_email")),
                    recipient_email=self._text(payload.get("recipient_email")),
                    subject=self._text(payload.get("subject")),
                    content=self._text(payload.get("content")),
                    cc_emails=self._text(payload.get("cc_emails")),
                    attachments=attachments,
                    message_id=scheduled.message_id,
                    reply_to_email_id=reply_to_email_id,
                    forward_from_email_id=forward_from_email_id,
                )
                scheduled.mailbox_email_id = sent_email.id
                if scheduled.lead_id is not None:
                    sent_payload = json.loads(self.cipher.decrypt(sent_email.encrypted_payload_json))
                    sent_payload["recipient_lead_id"] = scheduled.lead_id
                    sent_email.encrypted_payload_json = self.cipher.encrypt(json.dumps(sent_payload, ensure_ascii=False))
        except Exception as exc:
            self.db.rollback()
            return self._record_failure(scheduled_id=scheduled_id, error=str(exc))

        scheduled = self.db.get(HubScheduledEmail, scheduled_id)
        if scheduled is None:
            self.db.commit()
            return ScheduledEmailProcessResult()
        queued_storage_keys = [attachment.storage_key for attachment in scheduled.attachments]
        scheduled.attachments.clear()
        scheduled.status = "sent"
        scheduled.sent_at = self._utc_now()
        scheduled.locked_at = None
        scheduled.last_error = None
        self.db.commit()
        for storage_key in queued_storage_keys:
            self.attachment_storage.remove(storage_key)
        return ScheduledEmailProcessResult(sent=1)

    def _remove_failed_customer_email(self, email_id: int | None) -> None:
        if email_id is None:
            return
        email = self.db.get(CustomerZohoEmail, email_id)
        if email is None:
            return
        storage_keys = [attachment.storage_key for attachment in email.stored_attachments]
        self.db.delete(email)
        self.db.flush()
        for storage_key in storage_keys:
            self.attachment_storage.remove(storage_key)

    def _record_failure(self, *, scheduled_id: int, error: str) -> ScheduledEmailProcessResult:
        scheduled = self.db.get(HubScheduledEmail, scheduled_id)
        if scheduled is None:
            return ScheduledEmailProcessResult()
        scheduled.attempt_count += 1
        scheduled.locked_at = None
        scheduled.last_error = (error.strip() or "Der Versand konnte nicht abgeschlossen werden.")[:2_000]
        if scheduled.attempt_count > len(_RETRY_DELAYS):
            scheduled.status = "failed"
            self.db.commit()
            return ScheduledEmailProcessResult(failed=1)
        scheduled.status = "retrying"
        scheduled.next_attempt_at = self._utc_now() + _RETRY_DELAYS[scheduled.attempt_count - 1]
        self.db.commit()
        return ScheduledEmailProcessResult(retried=1)

    def _recover_stalled(self, *, now: datetime) -> tuple[int, int]:
        stale = list(
            self.db.scalars(
                select(HubScheduledEmail)
                .where(HubScheduledEmail.status == "sending")
                .where(HubScheduledEmail.locked_at.is_not(None))
                .where(HubScheduledEmail.locked_at <= now - _STALE_SENDING_AFTER)
            )
        )
        retried = failed = 0
        for scheduled in stale:
            scheduled.attempt_count += 1
            scheduled.locked_at = None
            scheduled.last_error = "Der Hub-Dienst wurde während des Versands neu gestartet."
            if scheduled.attempt_count > len(_RETRY_DELAYS):
                scheduled.status = "failed"
                failed += 1
            else:
                scheduled.status = "retrying"
                scheduled.next_attempt_at = now
                retried += 1
        if retried or failed:
            self.db.commit()
        return retried, failed

    def _editable(self, *, scheduled_email_id: int, action: str = "view") -> HubScheduledEmail:
        from app.core.mailbox_actor import resolve_mailbox_actor
        from app.services.hub_mailbox_access import HubMailboxAccess
        actor = resolve_mailbox_actor()
        if actor:
            HubMailboxAccess(db=self.db, cipher=self.cipher, actor=actor).require(f"scheduled-{scheduled_email_id}", action=action)
        scheduled = self.db.get(HubScheduledEmail, scheduled_email_id)
        if scheduled is None or scheduled.status not in _VISIBLE_STATUSES:
            raise ValueError("Die geplante E-Mail wurde nicht gefunden.")
        if scheduled.status == "sending":
            raise ValueError("Die E-Mail wird gerade versendet und kann nicht mehr geändert werden.")
        return scheduled

    @staticmethod
    def _optional_int(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _naive_utc(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
