"""Unified mailbox views for linked customer mail and unassigned Zoho workflow mail."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from html import escape
from hashlib import sha256
from secrets import token_hex

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only, selectinload

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxAttachment, HubMailboxEmail
from app.models.hub_scheduled_email import HubScheduledEmail
from app.services.customer_communications import (
    CustomerCommunicationAttachment,
    CustomerCommunicationAttachmentDownload,
    CustomerCommunicationAttachmentUpload,
    CustomerCommunicationService,
)
from app.services.email_attachment_storage import EmailAttachmentStorage, EmailAttachmentStorageError, track_attachment_file
from app.services.email_compose_images import EmailComposeImageError, EmailComposeImageService
from app.services.email_composer_settings import EmailComposerSettingsService
from app.services.email_html_compiler import EmailHtmlCompiler
from app.services.hub_mailbox_transport import (
    HubMailboxTransportAttachment,
    HubMailboxTransportError,
    HubMailboxTransportInlineImage,
    HubMailboxTransportService,
)
from app.services.hub_spam_senders import HubSpamSenderService
from app.services.hub_mailbox_access import HubMailboxAccess
from app.core.mailbox_actor import resolve_mailbox_actor
from app.services.hub_mailbox_permissions import bind_message
from app.services.hub_email_associations import EmailAssociationService, EmailRecordLink, index_email


MAILBOX_FOLDERS = frozenset({"inbox", "sent", "drafts", "planned", "unassigned", "trash", "spam"})
_ACTIVE_MAILBOX_STATE = "active"
_DRAFT_MAILBOX_STATE = "draft"
_DRAFT_SOURCE = "hub-draft"
_DIRECT_SEND_SOURCE = "hub-direct-send"
TASK_EMAIL_REMINDER_SOURCE = "hub-task-reminder"
MAILBOX_HEALTH_ALERT_SOURCE = "hub-mailbox-health-alert"
_MAILBOX_STATE_BY_FOLDER = {"drafts": _DRAFT_MAILBOX_STATE, "trash": "trash", "spam": "spam"}
_PLANNED_MAILBOX_STATUSES = ("scheduled", "retrying", "sending", "failed")
MAILBOX_ACTIONS = {
    "mark_read": "Als gelesen markieren",
    "mark_unread": "Als ungelesen markieren",
    "move_inbox": "In Posteingang verschieben",
    "move_sent": "In Gesendet verschieben (kein Versand)",
    "move_trash": "In Papierkorb verschieben",
    "move_spam": "Als Spam markieren und Absender sperren",
    "restore": "Wiederherstellen, Spam-Absender gegebenenfalls entsperren",
    "permanently_delete": "Aus Papierkorb endgueltig loeschen",
}
_BATCH_ACTIONS = frozenset(MAILBOX_ACTIONS)


@dataclass(frozen=True)
class HubMailboxCustomerLink:
    id: int
    name: str


@dataclass(frozen=True)
class HubMailboxLeadLink:
    id: int
    name: str


@dataclass(frozen=True)
class HubMailboxMessage:
    key: str
    kind: str
    subject: str
    sender: str | None
    recipients: str | None
    cc_recipients: str | None
    direction: str
    is_unread: bool
    mailbox_state: str
    occurred_at: datetime | None
    customers: tuple[HubMailboxCustomerLink, ...]
    customer_id: int | None
    customer_email_id: int | None
    preview_html: str | None
    attachments: tuple[CustomerCommunicationAttachment, ...]
    can_load_content: bool
    last_error: str | None
    scheduled_email_id: int | None = None
    lead: HubMailboxLeadLink | None = None
    record_links: tuple[EmailRecordLink, ...] = ()
    message_id: str | None = None


@dataclass(frozen=True)
class HubMailboxListItem:
    """The minimal data needed to render one row in the mailbox list."""

    key: str
    kind: str
    subject: str
    sender: str | None
    recipients: str | None
    direction: str
    is_unread: bool
    mailbox_state: str
    occurred_at: datetime | None
    customers: tuple[HubMailboxCustomerLink, ...]
    record_links: tuple[EmailRecordLink, ...] = ()


@dataclass(frozen=True)
class HubMailboxView:
    messages: tuple[HubMailboxListItem, ...]
    selected: HubMailboxMessage | None
    folder_counts: dict[str, int]


class HubMailboxService:
    """Present one mailbox while keeping customer email storage and permissions intact."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        public_base_url: str,
        attachment_storage: EmailAttachmentStorage | None = None,
        actor: str | None = None,
        account_id: int | None = None,
    ) -> None:
        self.db = db
        self.cipher = cipher
        actor = resolve_mailbox_actor(actor)
        self.actor = actor
        if account_id is not None and actor is None:
            raise ValueError("Fuer die Postfachauswahl fehlt die Berechtigung.")
        self.account_id = account_id
        self.scope = HubMailboxAccess(db=db, cipher=cipher, actor=actor, account_id=account_id) if actor is not None else None
        self.associations = self.scope.associations if self.scope else EmailAssociationService(db=db, cipher=cipher)
        self.communications = CustomerCommunicationService(
            db=db,
            cipher=cipher,
            public_base_url=public_base_url,
            attachment_storage=attachment_storage,
            actor=actor,
        )

    def _record_links(self, payload, direction):
        return tuple(link for link in self.associations.links(payload, direction)
                     if self.scope is None or self.scope.record_visible(link.module, link.id))

    def related_messages(self, *, module, record_id):
        if self.scope is not None and not self.scope.record_visible(module, record_id):
            return ()
        records = self.associations.records_for(module, record_id)
        messages, seen = [], set()
        for row in records:
            if self.scope is not None and not self.scope.visible(row):
                continue
            payload = self._payload(row.encrypted_payload_json)
            identity = getattr(row, "zoho_message_id", None) or payload.get("message_id")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            message = self._linked_message([row]) if isinstance(row, CustomerZohoEmail) else self._unassigned_message(row)
            messages.append(message)
        return tuple(sorted(messages, key=lambda message: CustomerCommunicationService._aware_utc(message.occurred_at) if message.occurred_at else datetime.min.replace(tzinfo=UTC), reverse=True))

    def get_view(self, *, folder: str, unread_only: bool, selected_key: str = "") -> HubMailboxView:
        """Load only the active folder; sidebar counts never need full email payloads."""
        folder_view = self.get_folder_view(
            folder=folder,
            unread_only=unread_only,
            selected_key=selected_key,
        )
        return HubMailboxView(
            messages=folder_view.messages,
            selected=folder_view.selected,
            folder_counts=self._folder_counts(),
        )

    def get_folder_counts(self) -> dict[str, int]:
        """Return folder counts restricted to the current user's visible records."""
        return self._folder_counts()

    def get_unread_count(self) -> int:
        linked = select(CustomerZohoEmail.zoho_message_id, CustomerZohoEmail.id).where(
            CustomerZohoEmail.direction == "inbound", CustomerZohoEmail.is_unread.is_(True), CustomerZohoEmail.mailbox_state == _ACTIVE_MAILBOX_STATE,
        )
        if self.scope is not None and self.scope.customers is not None:
            linked = linked.where(CustomerZohoEmail.customer_id.in_(self.scope.customers))
        if self.scope is not None:
            linked = self.scope.mailboxes.filter_statement(linked, CustomerZohoEmail)
        linked_count = len({message_id or f"local-{email_id}" for message_id, email_id in self.db.execute(linked)})
        filters = (HubMailboxEmail.direction == "inbound", HubMailboxEmail.is_unread.is_(True), HubMailboxEmail.mailbox_state == _ACTIVE_MAILBOX_STATE)
        if self.scope is not None and (self.scope.user.role != "admin" or self.account_id is not None):
            local_count = sum(self.scope.visible(row) for row in self.db.scalars(select(HubMailboxEmail).where(*filters)))
        else:
            local_count = self.db.scalar(select(func.count(HubMailboxEmail.id)).where(*filters)) or 0
        return linked_count + local_count

    def get_folder_view(self, *, folder: str, unread_only: bool, selected_key: str = "", load_selected: bool = True) -> HubMailboxView:
        """Load one folder for in-page navigation without rebuilding the other folders."""
        if folder not in MAILBOX_FOLDERS:
            raise ValueError("Unbekannter E-Mail-Ordner.")

        if folder == "unassigned":
            messages = [
                message
                for message in self._unassigned_list_messages(mailbox_state=_ACTIVE_MAILBOX_STATE)
                if message.kind == "unassigned"
            ]
        elif folder == "drafts":
            messages = self._unassigned_list_messages(mailbox_state=_DRAFT_MAILBOX_STATE)
        elif folder == "planned":
            messages = self._scheduled_list_messages()
        elif folder in _MAILBOX_STATE_BY_FOLDER:
            mailbox_state = _MAILBOX_STATE_BY_FOLDER[folder]
            messages = self._linked_list_messages(mailbox_state=mailbox_state) + self._unassigned_list_messages(mailbox_state=mailbox_state)
        else:
            direction = "inbound" if folder == "inbox" else "outbound"
            messages = self._linked_list_messages(direction=direction, mailbox_state=_ACTIVE_MAILBOX_STATE) + self._unassigned_list_messages(direction=direction, mailbox_state=_ACTIVE_MAILBOX_STATE)
        if unread_only:
            messages = [message for message in messages if message.is_unread]
        messages.sort(
            key=lambda message: (message.occurred_at.timestamp() if message.occurred_at else 0, message.key),
            reverse=True,
        )
        selected_item = next((message for message in messages if message.key == selected_key), messages[0] if messages else None)
        selected = self.get_selected_message(
            folder=folder,
            unread_only=unread_only,
            selected_key=selected_item.key,
        ) if selected_item and load_selected else None
        return HubMailboxView(messages=tuple(messages), selected=selected, folder_counts={})

    def get_selected_message(
        self,
        *,
        folder: str,
        unread_only: bool,
        selected_key: str,
    ) -> HubMailboxMessage | None:
        """Load one reading-pane message without rebuilding the complete mailbox list."""
        if folder not in MAILBOX_FOLDERS or not selected_key:
            return None
        if self.scope is not None:
            try:
                self.scope.require(selected_key)
            except ValueError:
                return None

        message: HubMailboxMessage | None = None
        if selected_key.startswith("linked-"):
            try:
                _, customer_id, email_id = selected_key.split("-", 2)
                selected_email = self.db.scalar(
                    select(CustomerZohoEmail)
                    .options(selectinload(CustomerZohoEmail.customer))
                    .where(
                        CustomerZohoEmail.customer_id == int(customer_id),
                        CustomerZohoEmail.id == int(email_id),
                    )
                    .limit(1)
                )
            except (TypeError, ValueError):
                selected_email = None
            if selected_email is not None:
                emails = [selected_email]
                if selected_email.zoho_message_id:
                    emails = self.db.scalars(
                        select(CustomerZohoEmail)
                        .options(selectinload(CustomerZohoEmail.customer))
                        .where(CustomerZohoEmail.zoho_message_id == selected_email.zoho_message_id)
                    ).all()
                emails = [email for email in emails if self.scope is None or self.scope.visible(email)]
                message = self._linked_message(emails)
        elif selected_key.startswith("unassigned-"):
            try:
                email_id = int(selected_key.removeprefix("unassigned-"))
            except ValueError:
                email_id = 0
            email = self.db.get(HubMailboxEmail, email_id)
            if email is not None:
                message = self._unassigned_message(email)
        elif selected_key.startswith("scheduled-"):
            try:
                scheduled_id = int(selected_key.removeprefix("scheduled-"))
            except ValueError:
                scheduled_id = 0
            scheduled = self.db.get(HubScheduledEmail, scheduled_id)
            if scheduled is not None and scheduled.status in _PLANNED_MAILBOX_STATUSES:
                message = self._scheduled_message(scheduled)

        if message is None or not self._matches_folder(message, folder):
            return None
        if unread_only and not message.is_unread:
            return None
        return message

    def mark_unassigned_read(self, *, email_id: int) -> None:
        if self.scope is not None:
            self.scope.require(f"unassigned-{email_id}")
        email = self.db.get(HubMailboxEmail, email_id)
        if email is None:
            raise ValueError("Die E-Mail wurde nicht gefunden.")
        email.is_unread = False
        self.db.flush()

    def apply_batch_action(self, *, keys: list[str], action: str) -> int:
        """Apply one mailbox action to a deduplicated selection of visible messages."""
        if action not in _BATCH_ACTIONS:
            raise ValueError("Unbekannte E-Mail-Aktion.")
        selected_keys = tuple(dict.fromkeys(key for key in keys if key))
        if not selected_keys:
            raise ValueError("Wähle mindestens eine E-Mail aus.")
        if len(selected_keys) > 1000:
            raise ValueError("Es können höchstens 1000 E-Mails gleichzeitig bearbeitet werden.")
        if self.scope is not None:
            for key in selected_keys:
                self.scope.require(key, "delete" if action in {"move_trash", "permanently_delete"} else "edit")

        linked_by_id: dict[int, CustomerZohoEmail] = {}
        unassigned_by_id: dict[int, HubMailboxEmail] = {}
        for key in selected_keys:
            if key.startswith("linked-"):
                try:
                    _, customer_id, email_id = key.split("-", 2)
                    email = self.db.scalar(
                        select(CustomerZohoEmail).where(
                            CustomerZohoEmail.customer_id == int(customer_id),
                            CustomerZohoEmail.id == int(email_id),
                        )
                    )
                except (TypeError, ValueError):
                    email = None
                if email is None:
                    continue
                # One Zoho message may be visible for several linked customers. Keep all copies aligned.
                related = (
                    self.db.scalars(
                        select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == email.zoho_message_id)
                    ).all()
                    if email.zoho_message_id
                    else [email]
                )
                for related_email in related:
                    if self.scope is None or self.scope.visible(related_email, "delete" if action in {"move_trash", "permanently_delete"} else "edit"):
                        linked_by_id[related_email.id] = related_email
            elif key.startswith("unassigned-"):
                try:
                    email_id = int(key.removeprefix("unassigned-"))
                except ValueError:
                    continue
                email = self.db.get(HubMailboxEmail, email_id)
                if email is not None:
                    unassigned_by_id[email.id] = email

        if not linked_by_id and not unassigned_by_id:
            raise ValueError("Die ausgewählten E-Mails wurden nicht gefunden.")

        selected_emails = (*linked_by_id.values(), *unassigned_by_id.values())
        if action == "permanently_delete":
            if any(email.mailbox_state != "trash" for email in selected_emails):
                raise ValueError("Nur E-Mails im Papierkorb können endgültig gelöscht werden.")
            storage_keys = [
                attachment.storage_key
                for email in selected_emails
                for attachment in email.stored_attachments
            ]
            for email in selected_emails:
                self.db.delete(email)
            self.db.flush()
            for storage_key in storage_keys:
                track_attachment_file(self.db, self.communications.attachment_storage, storage_key, removed=True)
            return len(selected_emails)

        spam_senders = HubSpamSenderService(db=self.db)
        for email in selected_emails:
            if action == "mark_read":
                email.is_unread = False
            elif action == "mark_unread":
                email.is_unread = True
            elif action == "move_inbox":
                email.mailbox_state = _ACTIVE_MAILBOX_STATE
                email.direction = "inbound"
            elif action == "move_sent":
                email.mailbox_state = _ACTIVE_MAILBOX_STATE
                email.direction = "outbound"
            elif action == "move_trash":
                email.mailbox_state = "trash"
            elif action == "move_spam":
                spam_senders.block(direction=email.direction, payload=self._payload(email.encrypted_payload_json))
                email.mailbox_state = "spam"
            elif action == "restore":
                if email.mailbox_state == "spam":
                    spam_senders.unblock(direction=email.direction, payload=self._payload(email.encrypted_payload_json))
                email.mailbox_state = _DRAFT_MAILBOX_STATE if getattr(email, "source", "") == _DRAFT_SOURCE else _ACTIVE_MAILBOX_STATE
            if action in {"move_inbox", "move_sent"}:
                index_email(self.db, self.cipher, email)
        self.db.flush()
        return len(linked_by_id) + len(unassigned_by_id)

    def save_draft(
        self,
        *,
        draft_id: int | None,
        sender_email: str,
        recipient_email: str,
        recipient_key: str,
        recipient_customer_id: int | None,
        recipient_lead_id: int | None = None,
        recipient_name: str,
        subject: str,
        content: str,
        cc_emails: str,
        template_id: str,
        reply_to_email_id: str,
        forward_from_email_id: str,
        dunning_id: int | None = None,
        scheduled_at: str = "",
        attachments: tuple[CustomerCommunicationAttachmentUpload, ...] = (),
        retained_attachment_ids: tuple[str, ...] | None = None,
        preserve_existing_links: bool = True,
        context_module: str = "",
        context_record_id: str = "",
    ) -> HubMailboxEmail:
        """Persist the editable fields of an unsent mailbox message in encrypted storage."""
        account = self.scope.mailboxes.require_sender(sender_email, "edit" if draft_id is not None else "create") if self.scope else None
        if recipient_lead_id is not None:
            from app.services.hub_deletion import lock_parent
            lock_parent(self.db, kind="leads", record_id=recipient_lead_id)
        if self.scope is not None and draft_id is not None:
            self.scope.require(f"unassigned-{draft_id}", "edit")
        if draft_id is None:
            draft = HubMailboxEmail(
                source=_DRAFT_SOURCE,
                direction="outbound",
                is_unread=False,
                mailbox_state=_DRAFT_MAILBOX_STATE,
                fingerprint=sha256(token_hex(32).encode("ascii")).hexdigest(),
                encrypted_payload_json="",
                received_at=datetime.now(UTC),
            )
            self.db.add(draft)
            self.db.flush()
        else:
            draft = self.db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.id == draft_id).with_for_update().execution_options(populate_existing=True))
            if draft is None or draft.source != _DRAFT_SOURCE or draft.mailbox_state != _DRAFT_MAILBOX_STATE:
                raise ValueError("Der Entwurf wurde nicht gefunden.")

        existing_payload = self._payload(draft.encrypted_payload_json) if draft_id is not None else {}
        existing_attachments = existing_payload.get("attachments")
        payload_attachments = list(existing_attachments) if isinstance(existing_attachments, list) else []
        if retained_attachment_ids is not None:
            self._validate_retained_attachment_ids(payload_attachments, retained_attachment_ids)
            payload_attachments = [item for item in payload_attachments if item["id"] in retained_attachment_ids]
        uploaded = self._validated_direct_attachments(attachments)
        stored_by_id = {item.source_attachment_id: item for item in self.db.scalars(
            select(HubMailboxAttachment).where(HubMailboxAttachment.email_id == draft.id)
        )}
        uploads_by_id = {self.draft_attachment_id(draft.id, item): item for item in uploaded}
        final_ids = {item["id"] for item in payload_attachments} | uploads_by_id.keys()
        if final_ids - stored_by_id.keys() - uploads_by_id.keys():
            raise ValueError("Ein gespeicherter Anhang ist nicht verfügbar. Bitte den Entwurf neu öffnen.")
        if len(final_ids) > 20:
            raise ValueError("Es können höchstens 20 Anhänge pro E-Mail gespeichert werden.")
        total_bytes = sum(
            len(uploads_by_id[key].content) if key in uploads_by_id else stored_by_id[key].byte_size
            for key in final_ids
        )
        if total_bytes > 50 * 1024 * 1024:
            raise ValueError("Die Anhänge sind zusammen größer als 50 MB.")
        storage_keys: list[str] = []
        try:
            if uploaded:
                self.communications.ensure_email_attachment_storage()
            for attachment_id, attachment in uploads_by_id.items():
                if attachment_id in stored_by_id:
                    if not any(item["id"] == attachment_id for item in payload_attachments):
                        payload_attachments.append({"id": attachment_id, "name": attachment.filename.strip()})
                    continue
                storage_key = self.communications.attachment_storage.store(attachment.content)
                storage_keys.append(storage_key)
                track_attachment_file(self.db, self.communications.attachment_storage, storage_key)
                self.db.add(HubMailboxAttachment(
                    email_id=draft.id, source=_DRAFT_SOURCE, source_attachment_id=attachment_id,
                    storage_key=storage_key, content_type=attachment.content_type or "application/octet-stream",
                    byte_size=len(attachment.content), stored_at=datetime.now(UTC),
                ))
                payload_attachments.append({"id": attachment_id, "name": attachment.filename.strip()[:255]})
        except Exception as exc:
            for storage_key in storage_keys:
                self.communications.attachment_storage.remove(storage_key)
            if isinstance(exc, EmailAttachmentStorageError):
                raise ValueError(str(exc)) from exc
            raise

        for attachment_id, stored in stored_by_id.items():
            if attachment_id not in final_ids:
                track_attachment_file(self.db, self.communications.attachment_storage, stored.storage_key, removed=True)
                self.db.delete(stored)

        payload = {
            "subject": subject.strip()[:500],
            "sender": sender_email.strip()[:320],
            "recipient_email": recipient_email.strip()[:320],
            "recipient_key": recipient_key.strip()[:255],
            "recipient_customer_id": (
                existing_payload.get("recipient_customer_id")
                if preserve_existing_links and recipient_customer_id is None and recipient_email.strip() == existing_payload.get("recipient_email")
                else recipient_customer_id
            ),
            "recipient_lead_id": recipient_lead_id if recipient_lead_id is not None or not preserve_existing_links else existing_payload.get("recipient_lead_id"),
            "dunning_id": dunning_id,
            "recipient_name": recipient_name.strip()[:255],
            "content": content[:500_000],
            "cc_emails": cc_emails.strip()[:2_000],
            "template_id": template_id.strip()[:255],
            "context_module": context_module.strip()[:64],
            "context_record_id": context_record_id.strip()[:64],
            "reply_to_email_id": reply_to_email_id.strip()[:64],
            "forward_from_email_id": forward_from_email_id.strip()[:64],
            "scheduled_at": scheduled_at.strip()[:32],
            "attachments": payload_attachments,
        }
        draft.direction = "outbound"
        draft.is_unread = False
        draft.mailbox_state = _DRAFT_MAILBOX_STATE
        draft.encrypted_payload_json = self.cipher.encrypt(json.dumps(payload, ensure_ascii=False))
        draft.received_at = datetime.now(UTC)
        draft.last_error = None
        self.db.flush()
        bind_message(self.db, self.cipher, draft, account_id=account.id if account else None, replace=True)
        return draft

    def send_direct_email(
        self,
        *,
        sender_email: str,
        recipient_email: str,
        subject: str,
        content: str,
        cc_emails: str,
        attachments: tuple[CustomerCommunicationAttachmentUpload, ...] = (),
        source: str = _DIRECT_SEND_SOURCE,
        message_id: str | None = None,
        reply_to_email_id: int | None = None,
        forward_from_email_id: int | None = None,
    ) -> HubMailboxEmail:
        """Send an email without a customer link and retain a local sent copy."""
        if self.scope is not None:
            self.scope.mailboxes.require_sender(sender_email, "send")
        if source not in {_DIRECT_SEND_SOURCE, TASK_EMAIL_REMINDER_SOURCE, MAILBOX_HEALTH_ALERT_SOURCE}:
            raise ValueError("Unbekannte Quelle für die Hub-E-Mail.")
        if reply_to_email_id is not None and forward_from_email_id is not None:
            raise ValueError("Eine E-Mail kann nicht gleichzeitig Antwort und Weiterleitung sein.")
        recipient_name, parsed_recipient_email = parseaddr(recipient_email.strip())
        normalized_recipient_email = parsed_recipient_email.strip().casefold()
        if not normalized_recipient_email or "@" not in normalized_recipient_email:
            raise ValueError("Gib eine gültige Empfängeradresse ein.")

        reply_to_message_id = self._unassigned_reply_message_id(
            email_id=reply_to_email_id,
            recipient_email=normalized_recipient_email,
        )
        forwarded_attachments = self._forwarded_unassigned_attachments(email_id=forward_from_email_id)

        transport = HubMailboxTransportService(db=self.db, cipher=self.cipher, actor=self.actor)
        sender = next(
            (item for item in transport.list_senders() if item.email.casefold() == sender_email.strip().casefold()),
            None,
        )
        if sender is None:
            raise ValueError("Wähle ein eingerichtetes Mittwald-Postfach als Absender aus.")

        normalized_subject = self.communications._required_text(subject, "Betreff", maximum=500)
        composer_settings_service = EmailComposerSettingsService(db=self.db)
        composer_settings = composer_settings_service.get_runtime_settings()
        normalized_content = composer_settings_service.apply_default_style(
            self.communications._sanitized_email_content(content)
        )
        source_stylesheet = EmailHtmlCompiler.extract_stylesheet(content)
        delivery_content, inline_images = EmailComposeImageService(
            db=self.db,
            cipher=self.cipher,
        ).prepare_inline_images(normalized_content)
        compilation = EmailHtmlCompiler().compile(
            delivery_content,
            source_stylesheet=source_stylesheet,
            font_family=composer_settings.font_family,
            font_size=composer_settings.font_size,
            line_height=composer_settings.line_height,
        )
        cc_recipients = self.communications._cc_recipients(
            cc_emails,
            excluded_emails={sender.email, normalized_recipient_email},
        )
        normalized_attachments = self._validated_direct_attachments((*attachments, *forwarded_attachments))
        now = datetime.now(UTC)
        payload_attachments = [
            {
                "id": sha256(token_hex(32).encode("ascii")).hexdigest(),
                "name": attachment.filename.strip()[:255],
            }
            for attachment in normalized_attachments
        ]
        email = HubMailboxEmail(
            source=source,
            direction="outbound",
            is_unread=False,
            mailbox_state=_ACTIVE_MAILBOX_STATE,
            fingerprint=sha256(token_hex(32).encode("ascii")).hexdigest(),
            encrypted_payload_json="",
            received_at=now,
        )
        self.db.add(email)
        self.db.flush()

        storage_keys: list[str] = []
        try:
            if normalized_attachments:
                self.communications.ensure_email_attachment_storage()
            for metadata, attachment in zip(payload_attachments, normalized_attachments, strict=True):
                storage_key = self.communications.attachment_storage.store(attachment.content)
                storage_keys.append(storage_key)
                self.db.add(
                    HubMailboxAttachment(
                        email_id=email.id,
                        source=source,
                        source_attachment_id=metadata["id"],
                        storage_key=storage_key,
                        content_type=(attachment.content_type or "application/octet-stream")[:128],
                        byte_size=len(attachment.content),
                        stored_at=now,
                    )
                )
            delivery = transport.send(
                sender_email=sender.email,
                recipient_name=(recipient_name.strip() or normalized_recipient_email)[:255],
                recipient_email=normalized_recipient_email,
                subject=normalized_subject,
                html_content=compilation.compiled_html,
                cc_recipients=cc_recipients,
                reply_to_message_id=reply_to_message_id,
                attachments=tuple(
                    HubMailboxTransportAttachment(
                        filename=attachment.filename,
                        content=attachment.content,
                        content_type=attachment.content_type,
                    )
                    for attachment in normalized_attachments
                ),
                inline_images=tuple(
                    HubMailboxTransportInlineImage(
                        content_id=image.content_id,
                        filename=image.filename,
                        content=image.content,
                        content_type=image.content_type,
                    )
                    for image in inline_images
                ),
                message_id=message_id,
            )
        except (EmailAttachmentStorageError, EmailComposeImageError, HubMailboxTransportError, ValueError) as exc:
            for storage_key in storage_keys:
                self.communications.attachment_storage.remove(storage_key)
            self.db.delete(email)
            self.db.flush()
            raise ValueError(str(exc)) from exc

        email.encrypted_payload_json = self.cipher.encrypt(json.dumps({
            "subject": normalized_subject,
            "content": normalized_content,
            "outbound_html": compilation.compiled_html,
            "email_compiler": {
                "mode": "hub",
                "version": compilation.version,
                "warnings": list(compilation.warnings),
            },
            "from": {"name": sender.name, "email": sender.email},
            "to": {"name": (recipient_name.strip() or normalized_recipient_email)[:255], "email": normalized_recipient_email},
            "cc": [{"name": name, "email": address} for name, address in cc_recipients],
            "attachments": payload_attachments,
            "message_id": delivery.message_id,
            "sent_time": delivery.sent_at.isoformat(),
            "in_reply_to": {
                "email_id": reply_to_email_id,
                "message_id": reply_to_message_id,
            } if reply_to_email_id is not None else None,
            "forwarded_from_email_id": forward_from_email_id,
        }, ensure_ascii=False))
        email.fingerprint = sha256(f"mittwald-imap:{delivery.message_id}".encode("utf-8")).hexdigest()
        index_email(self.db, self.cipher, email)
        self.db.flush()
        return email

    def get_draft_compose_context(self, *, draft_id: int) -> dict[str, object]:
        if self.scope is not None:
            self.scope.require(f"unassigned-{draft_id}")
        draft = self.db.get(HubMailboxEmail, draft_id)
        if draft is None or draft.source != _DRAFT_SOURCE or draft.mailbox_state != _DRAFT_MAILBOX_STATE:
            raise ValueError("Der Entwurf wurde nicht gefunden.")
        payload = self._payload(draft.encrypted_payload_json)
        recipient_key = self._text(payload.get("recipient_key")) or ""
        recipient_email = self._text(payload.get("recipient_email")) or ""
        recipient_name = self._text(payload.get("recipient_name")) or ""
        customer_id_value = payload.get("recipient_customer_id")
        try:
            recipient_customer_id = int(customer_id_value) if customer_id_value is not None else None
        except (TypeError, ValueError):
            recipient_customer_id = None
        dunning_id_value = payload.get("dunning_id")
        try:
            dunning_id = int(dunning_id_value) if dunning_id_value is not None else None
        except (TypeError, ValueError):
            dunning_id = None
        lead_id_value = payload.get("recipient_lead_id")
        try:
            lead_id = int(lead_id_value) if lead_id_value is not None else None
        except (TypeError, ValueError):
            lead_id = None
        if lead_id is not None:
            from app.models.hub_lead import HubLead
            if self.db.get(HubLead, lead_id) is None:
                lead_id = None
                if payload.get("context_module") in {"lead", "leads"}:
                    payload["context_module"] = payload["context_record_id"] = ""
        recipient = (
            {
                "key": recipient_key,
                "email": recipient_email,
                "name": recipient_name or recipient_email,
            }
            if recipient_key and recipient_email and recipient_customer_id is not None
            else None
        )
        return {
            "action": "draft",
            "draft_id": draft.id,
            "sender_email": self._text(payload.get("sender")) or "",
            "recipient": recipient,
            "recipient_email": recipient_email,
            "customer_id": recipient_customer_id,
            "lead_id": lead_id,
            "dunning_id": dunning_id,
            "subject": self._text(payload.get("subject")) or "",
            "content": self._text(payload.get("content")) or "",
            "cc_emails": self._text(payload.get("cc_emails")) or "",
            "template_id": self._text(payload.get("template_id")) or "",
            "context_module": self._text(payload.get("context_module")) or "",
            "context_record_id": self._text(payload.get("context_record_id")) or "",
            "reply_to_email_id": self._text(payload.get("reply_to_email_id")) or "",
            "forward_from_email_id": self._text(payload.get("forward_from_email_id")) or "",
            "scheduled_at": self._text(payload.get("scheduled_at")) or "",
            "attachments": [
                {"id": attachment.id, "filename": attachment.filename}
                for attachment in self.communications._email_attachments(payload)
            ],
        }

    @staticmethod
    def draft_attachment_id(draft_id: int, attachment: CustomerCommunicationAttachmentUpload) -> str:
        metadata = json.dumps([draft_id, attachment.filename.strip(), attachment.content_type], ensure_ascii=False)
        return sha256(metadata.encode("utf-8") + b"\0" + attachment.content).hexdigest()

    @staticmethod
    def parse_retained_attachment_ids(value: str) -> tuple[str, ...] | None:
        if not value:
            return None
        try:
            ids = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Die Anhangsauswahl ist ungültig.") from exc
        if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(item, str) or not item or len(item) > 255 for item in ids):
            raise ValueError("Die Anhangsauswahl ist ungültig.")
        return tuple(dict.fromkeys(ids))

    @staticmethod
    def _validate_retained_attachment_ids(attachments: list[dict], ids: tuple[str, ...]) -> None:
        if set(ids) - {item["id"] for item in attachments}:
            raise ValueError("Ein ausgewählter Anhang gehört nicht zu diesem Entwurf. Bitte den Entwurf neu öffnen.")

    def draft_attachments(
        self, *, draft_id: int, retained_attachment_ids: tuple[str, ...] | None = None,
    ) -> tuple[CustomerCommunicationAttachmentUpload, ...]:
        if self.scope is not None:
            self.scope.require(f"unassigned-{draft_id}")
        draft = self.db.get(HubMailboxEmail, draft_id)
        if draft is None or draft.source != _DRAFT_SOURCE or draft.mailbox_state != _DRAFT_MAILBOX_STATE:
            raise ValueError("Der Entwurf wurde nicht gefunden.")
        payload = self._payload(draft.encrypted_payload_json)
        if retained_attachment_ids is not None:
            self._validate_retained_attachment_ids(payload.get("attachments", []), retained_attachment_ids)
        return tuple(
            CustomerCommunicationAttachmentUpload(
                filename=download.filename, content=download.content, content_type=download.content_type,
            )
            for attachment in self.communications._email_attachments(payload)
            if retained_attachment_ids is None or attachment.id in retained_attachment_ids
            for download in (self.download_unassigned_attachment(email_id=draft.id, attachment_id=attachment.id),)
        )

    def get_unassigned_email_compose_context(self, *, email_id: int, action: str) -> dict[str, object]:
        """Build reply or forward content for any incoming mailbox message without a customer link."""
        if action not in {"reply", "reply_all", "forward"}:
            raise ValueError("Unbekannte E-Mail-Aktion.")
        email = self._require_inbound_unassigned_email(email_id=email_id)
        payload = self._payload(email.encrypted_payload_json)
        original_content = self._unassigned_content(payload) or ""
        original_subject = self._text(payload.get("betreff")) or self._text(payload.get("subject")) or "Ohne Betreff"
        subject = original_subject
        original_sender = self._people(
            payload.get("absender") or payload.get("sender") or payload.get("from")
        ) or "Unbekannt"

        if action in {"reply", "reply_all"}:
            sender_addresses = self.communications._email_addresses(
                payload.get("absender") or payload.get("sender") or payload.get("from")
            )
            if len(sender_addresses) != 1:
                raise ValueError("Der Absender dieser E-Mail ist nicht eindeutig.")
            recipient_email = sender_addresses[0]
            if not re.match(r"^\s*re\s*:", subject, flags=re.IGNORECASE):
                subject = f"Re: {subject}"
            signature_html = EmailComposerSettingsService(db=self.db).render_signature(actor=self.actor)
            signature_section = f"{signature_html}<p><br><br></p>" if signature_html else ""
            quoted_content = self.communications._sanitized_email_content(original_content) if original_content else ""
            content = (
                "<br><br>"
                f"{signature_section}"
                f"<p><strong>Am {escape(email.received_at.isoformat())} schrieb {escape(original_sender)}:</strong></p>"
                "<p><br></p>"
                '<blockquote style="margin: 0 0 0 0.8ex; border-left: 1px solid #c7c7c7; padding-left: 1em;">'
                f"{quoted_content}</blockquote>"
            )
            if len(content) > 50_000:
                raise ValueError("Die ursprüngliche E-Mail ist zu groß, um sie vollständig zu zitieren.")
            cc_emails = sorted(
                (
                    set(self.communications._email_addresses(payload.get("to")))
                    | set(self.communications._email_addresses(payload.get("cc")))
                ) - {recipient_email}
            ) if action == "reply_all" else []
            return {
                "action": action,
                "customer_id": None,
                "recipient": None,
                "recipient_email": recipient_email,
                "subject": subject[:500],
                "content": content,
                "cc_emails": cc_emails,
                "reply_to_email_id": email.id,
                "forward_from_email_id": None,
            }

        if not re.match(r"^\s*fwd\s*:", subject, flags=re.IGNORECASE):
            subject = f"Fwd: {subject}"
        original_recipients = self._people(
            payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient") or payload.get("to")
        ) or "Unbekannt"
        metadata = (
            "<hr><p><strong>Weitergeleitete Nachricht</strong><br>"
            f"Von: {escape(original_sender)}<br>"
            f"An: {escape(original_recipients)}<br>"
            f"Datum: {escape(email.received_at.isoformat())}<br>"
            f"Betreff: {escape(original_subject)}</p>"
        )
        content = f"<p><br></p>{metadata}{self.communications._sanitized_email_content(original_content) if original_content else ''}"
        if len(content) > 50_000:
            raise ValueError("Die ursprüngliche E-Mail ist zu groß, um sie vollständig weiterzuleiten.")
        return {
            "action": "forward",
            "customer_id": None,
            "recipient": None,
            "recipient_email": "",
            "subject": subject[:500],
            "content": content,
            "cc_emails": [],
            "reply_to_email_id": None,
            "forward_from_email_id": email.id,
        }

    def _require_inbound_unassigned_email(self, *, email_id: int) -> HubMailboxEmail:
        if self.scope is not None:
            self.scope.require(f"unassigned-{email_id}")
        email = self.db.get(HubMailboxEmail, email_id)
        if email is None or email.direction != "inbound":
            raise ValueError("Nur auf eingegangene E-Mails kann geantwortet oder weitergeleitet werden.")
        return email

    def _unassigned_reply_message_id(self, *, email_id: int | None, recipient_email: str) -> str | None:
        if email_id is None:
            return None
        email = self._require_inbound_unassigned_email(email_id=email_id)
        payload = self._payload(email.encrypted_payload_json)
        sender_addresses = self.communications._email_addresses(
            payload.get("absender") or payload.get("sender") or payload.get("from")
        )
        if len(sender_addresses) != 1 or sender_addresses[0] != recipient_email:
            raise ValueError("Eine Antwort muss an den Absender der ursprünglichen E-Mail gesendet werden.")
        message_id = self._text(payload.get("mittwald_message_id") or payload.get("message_id"))
        if message_id and re.fullmatch(r"<[^<>\r\n]{1,498}>", message_id):
            return message_id
        return None

    def _forwarded_unassigned_attachments(
        self,
        *,
        email_id: int | None,
    ) -> tuple[CustomerCommunicationAttachmentUpload, ...]:
        if email_id is None:
            return ()
        email = self._require_inbound_unassigned_email(email_id=email_id)
        payload = self._payload(email.encrypted_payload_json)
        attachments: list[CustomerCommunicationAttachmentUpload] = []
        for attachment in self.communications._email_attachments(payload):
            downloaded = self.download_unassigned_attachment(email_id=email.id, attachment_id=attachment.id)
            attachments.append(
                CustomerCommunicationAttachmentUpload(
                    filename=downloaded.filename,
                    content=downloaded.content,
                    content_type=downloaded.content_type,
                )
            )
        return tuple(attachments)

    def prepare_draft_delivery_attachments(self, *, draft_id: int, retained_attachment_ids=None):
        if self.scope is not None:
            self.scope.require(f"unassigned-{draft_id}", action="send")
        return self.draft_attachments(draft_id=draft_id, retained_attachment_ids=retained_attachment_ids)

    def discard_draft(self, *, draft_id: int) -> bool:
        if self.scope is not None:
            # This is the post-delivery cleanup, not a separate mailbox delete action.
            self.scope.require(f"unassigned-{draft_id}", action="send")
        draft = self.db.get(HubMailboxEmail, draft_id)
        if draft is None or draft.source != _DRAFT_SOURCE or draft.mailbox_state != _DRAFT_MAILBOX_STATE:
            return False
        for attachment in self.db.scalars(select(HubMailboxAttachment).where(HubMailboxAttachment.email_id == draft.id)):
            track_attachment_file(self.db, self.communications.attachment_storage, attachment.storage_key, removed=True)
        self.db.delete(draft)
        self.db.flush()
        return True

    def _linked_list_messages(
        self,
        *,
        direction: str | None = None,
        mailbox_state: str = _ACTIVE_MAILBOX_STATE,
    ) -> list[HubMailboxListItem]:
        """Load headers for the mailbox list without constructing every email preview."""
        statement = (
            select(CustomerZohoEmail)
            .options(
                load_only(
                    CustomerZohoEmail.id,
                    CustomerZohoEmail.customer_id,
                    CustomerZohoEmail.zoho_message_id,
                    CustomerZohoEmail.direction,
                    CustomerZohoEmail.is_unread,
                    CustomerZohoEmail.mailbox_state,
                    CustomerZohoEmail.zoho_sent_at,
                    CustomerZohoEmail.created_at,
                    CustomerZohoEmail.encrypted_header_json,
                ),
                selectinload(CustomerZohoEmail.customer).load_only(Customer.id, Customer.name),
            )
            .order_by(CustomerZohoEmail.zoho_sent_at.desc(), CustomerZohoEmail.id.desc())
        )
        statement = statement.where(CustomerZohoEmail.mailbox_state == mailbox_state)
        if self.scope is not None and self.scope.customers is not None:
            statement = statement.where(CustomerZohoEmail.customer_id.in_(self.scope.customers))
        if self.scope is not None:
            statement = self.scope.mailboxes.filter_statement(statement, CustomerZohoEmail)
        if direction is not None:
            statement = statement.where(CustomerZohoEmail.direction == direction)
        rows = self.db.scalars(statement).all()
        groups: dict[str, list[CustomerZohoEmail]] = {}
        for email in rows:
            group_key = email.zoho_message_id or f"local-{email.id}"
            groups.setdefault(group_key, []).append(email)

        messages: list[HubMailboxListItem] = []
        for emails in groups.values():
            messages.append(self._linked_list_message(emails))
        return messages

    def _unassigned_list_messages(
        self,
        *,
        direction: str | None = None,
        mailbox_state: str = _ACTIVE_MAILBOX_STATE,
    ) -> list[HubMailboxListItem]:
        statement = select(HubMailboxEmail).order_by(HubMailboxEmail.received_at.desc(), HubMailboxEmail.id.desc())
        statement = statement.where(HubMailboxEmail.mailbox_state == mailbox_state)
        if direction is not None:
            statement = statement.where(HubMailboxEmail.direction == direction)
        return [
            self._unassigned_list_message(email)
            for email in self.db.scalars(statement).all()
            if self.scope is None or self.scope.visible(email)
        ]

    def _scheduled_list_messages(self) -> list[HubMailboxListItem]:
        scheduled_rows = list(
            self.db.scalars(
                select(HubScheduledEmail)
                .where(HubScheduledEmail.status.in_(_PLANNED_MAILBOX_STATUSES))
                .order_by(HubScheduledEmail.scheduled_at.desc(), HubScheduledEmail.id.desc())
            ).all()
        )
        if self.scope is not None:
            scheduled_rows = [row for row in scheduled_rows if self.scope.visible(row)]
        customer_ids = {row.customer_id for row in scheduled_rows if row.customer_id is not None}
        customers = (
            {
                customer.id: customer
                for customer in self.db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
            }
            if customer_ids
            else {}
        )
        return [self._scheduled_list_message(row, customers.get(row.customer_id)) for row in scheduled_rows]

    def _scheduled_list_message(
        self,
        scheduled: HubScheduledEmail,
        customer: Customer | None,
    ) -> HubMailboxListItem:
        payload = self._payload(scheduled.encrypted_payload_json)
        return HubMailboxListItem(
            key=f"scheduled-{scheduled.id}",
            kind="scheduled",
            subject=self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._text(payload.get("sender_email")),
            recipients=self._text(payload.get("recipient_email")),
            direction="outbound",
            is_unread=False,
            mailbox_state=scheduled.status,
            occurred_at=scheduled.scheduled_at,
            customers=(HubMailboxCustomerLink(id=customer.id, name=customer.name),) if customer is not None else (),
        )

    def _linked_list_message(self, emails: list[CustomerZohoEmail]) -> HubMailboxListItem:
        # The full view may parse HTML and attachments. A list row reads only its encrypted header.
        winner = max(emails, key=lambda email: email.id)
        payload = self._email_list_header(winner)
        customers = self._linked_customers(emails)
        return HubMailboxListItem(
            key=f"linked-{winner.customer_id}-{winner.id}",
            kind="linked",
            subject=self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._text(payload.get("sender")),
            recipients=self._text(payload.get("recipients")),
            direction=winner.direction,
            is_unread=any(email.is_unread and email.direction == "inbound" for email in emails),
            mailbox_state=winner.mailbox_state,
            occurred_at=winner.zoho_sent_at or winner.created_at,
            customers=customers,
        )

    def _unassigned_list_message(self, email: HubMailboxEmail) -> HubMailboxListItem:
        payload = self._payload(email.encrypted_payload_json)
        kind = self._unassigned_kind(email)
        links = self._record_links(payload, email.direction) if kind != "draft" else ()
        return HubMailboxListItem(
            key=f"unassigned-{email.id}",
            kind="associated" if kind == "unassigned" and links else kind,
            subject=self._text(payload.get("betreff")) or self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._people(payload.get("absender") or payload.get("sender") or payload.get("from")),
            recipients=self._people(payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient") or payload.get("to")),
            direction=email.direction,
            is_unread=email.is_unread,
            mailbox_state=email.mailbox_state,
            occurred_at=email.received_at,
            customers=(),
            record_links=links,
        )

    def _folder_counts(self) -> dict[str, int]:
        """Filter references before counting, without constructing message previews."""
        linked_statement = select(
            CustomerZohoEmail.mailbox_state, CustomerZohoEmail.direction,
            CustomerZohoEmail.zoho_message_id, CustomerZohoEmail.id,
        )
        if self.scope is not None and self.scope.customers is not None:
            linked_statement = linked_statement.where(CustomerZohoEmail.customer_id.in_(self.scope.customers))
        if self.scope is not None:
            linked_statement = self.scope.mailboxes.filter_statement(linked_statement, CustomerZohoEmail)
        linked_keys = {
            (mailbox_state, direction, message_id or f"local-{email_id}")
            for mailbox_state, direction, message_id, email_id in self.db.execute(linked_statement)
        }
        restricted = self.scope is not None and (self.scope.user.role != "admin" or self.account_id is not None)
        # Use the same classification and scope as the list, including automatic CRM links.
        unassigned_rows, unknown_ids = [], set()
        for row in self.db.scalars(select(HubMailboxEmail)):
            if restricted and not self.scope.visible(row):
                continue
            unassigned_rows.append((row.mailbox_state, row.direction, row.source, row.id))
            if self._unassigned_kind(row) == "unassigned" and not self._record_links(self._payload(row.encrypted_payload_json), row.direction):
                unknown_ids.add(row.id)
        active_unassigned_rows = [
            (direction, source, email_id)
            for mailbox_state, direction, source, email_id in unassigned_rows
            if mailbox_state == _ACTIVE_MAILBOX_STATE
        ]
        planned_count = sum(self.scope.visible(row) for row in self.db.scalars(
            select(HubScheduledEmail).where(HubScheduledEmail.status.in_(_PLANNED_MAILBOX_STATUSES))
        )) if restricted else self.db.scalar(
            select(func.count(HubScheduledEmail.id)).where(HubScheduledEmail.status.in_(_PLANNED_MAILBOX_STATUSES))
        ) or 0
        return {
            "inbox": sum(
                mailbox_state == _ACTIVE_MAILBOX_STATE and direction == "inbound"
                for mailbox_state, direction, _ in linked_keys
            ) + sum(direction == "inbound" for direction, _source, _ in active_unassigned_rows),
            "sent": sum(
                mailbox_state == _ACTIVE_MAILBOX_STATE and direction == "outbound"
                for mailbox_state, direction, _ in linked_keys
            ) + sum(direction == "outbound" for direction, _source, _ in active_unassigned_rows),
            "unassigned": sum(email_id in unknown_ids for _direction, _source, email_id in active_unassigned_rows),
            "trash": sum(mailbox_state == "trash" for mailbox_state, _, _ in linked_keys)
            + sum(mailbox_state == "trash" for mailbox_state, _, _source, _ in unassigned_rows),
            "spam": sum(mailbox_state == "spam" for mailbox_state, _, _ in linked_keys)
            + sum(mailbox_state == "spam" for mailbox_state, _, _source, _ in unassigned_rows),
            "drafts": sum(
                mailbox_state == _DRAFT_MAILBOX_STATE and source == _DRAFT_SOURCE
                for mailbox_state, _direction, source, _email_id in unassigned_rows
            ),
            "planned": planned_count,
        }

    def _email_list_header(self, email: CustomerZohoEmail) -> dict[str, object]:
        header = self._payload(email.encrypted_header_json)
        if header:
            return header
        # Existing rows are backfilled on startup. This fallback keeps the list readable if a migration is interrupted.
        return self._payload(email.encrypted_payload_json)

    def _linked_message(self, emails: list[CustomerZohoEmail]) -> HubMailboxMessage:
        winner = max(
            emails,
            key=lambda email: (bool(self.communications._email_view(email).preview_html), email.id),
        )
        view = self.communications._email_view(winner)
        customers = self._linked_customers(emails)
        return HubMailboxMessage(
            key=f"linked-{winner.customer_id}-{winner.id}",
            kind="linked",
            subject=view.subject,
            sender=view.sender,
            recipients=view.recipients,
            cc_recipients=self._people(self._payload(winner.encrypted_payload_json).get("cc")),
            direction=view.direction,
            is_unread=any(email.is_unread and email.direction == "inbound" for email in emails),
            mailbox_state=winner.mailbox_state,
            occurred_at=view.occurred_at,
            customers=customers,
            customer_id=winner.customer_id,
            customer_email_id=winner.id,
            preview_html=view.preview_html,
            attachments=view.attachments,
            can_load_content=view.can_load_content,
            last_error=view.last_error,
            record_links=self._record_links(self._payload(winner.encrypted_payload_json), winner.direction),
            message_id=winner.zoho_message_id,
        )

    def _unassigned_message(self, email: HubMailboxEmail) -> HubMailboxMessage:
        payload = self._payload(email.encrypted_payload_json)
        content = self._unassigned_content(payload)
        kind = self._unassigned_kind(email)
        links = self._record_links(payload, email.direction) if kind != "draft" else ()
        customer = None
        lead = None
        if kind == "draft":
            context = self.get_draft_compose_context(draft_id=email.id)
            if context["customer_id"] is not None:
                customer = self.db.get(Customer, context["customer_id"])
            if context["lead_id"] is not None:
                # Lead workflows also use the mailbox for reminders.
                from app.services.hub_leads import HubLeadService

                lead = HubLeadService(db=self.db, cipher=self.cipher).get_detail(lead_id=context["lead_id"])
        return HubMailboxMessage(
            key=f"unassigned-{email.id}",
            kind="associated" if kind == "unassigned" and links else kind,
            subject=self._text(payload.get("betreff")) or self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._people(payload.get("absender") or payload.get("sender") or payload.get("from")),
            recipients=self._people(payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient") or payload.get("to")),
            cc_recipients=self._people(payload.get("cc")),
            direction=email.direction,
            is_unread=email.is_unread,
            mailbox_state=email.mailbox_state,
            occurred_at=email.received_at,
            customers=(HubMailboxCustomerLink(id=customer.id, name=customer.name),) if customer is not None else (),
            customer_id=customer.id if customer is not None else None,
            lead=HubMailboxLeadLink(id=lead.lead.id, name=lead.name) if lead is not None else None,
            customer_email_id=email.id,
            # Deluge can send an unknown email body directly; render it only inside the sandboxed preview.
            preview_html=CustomerCommunicationService._email_preview_document(content),
            attachments=self.communications._email_attachments(payload),
            can_load_content=False,
            last_error=email.last_error,
            record_links=links,
            message_id=self._text(payload.get("message_id")),
        )

    def _scheduled_message(self, scheduled: HubScheduledEmail) -> HubMailboxMessage:
        payload = self._payload(scheduled.encrypted_payload_json)
        customer = self.db.get(Customer, scheduled.customer_id) if scheduled.customer_id is not None else None
        return HubMailboxMessage(
            key=f"scheduled-{scheduled.id}",
            kind="scheduled",
            subject=self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._text(payload.get("sender_email")),
            recipients=self._text(payload.get("recipient_email")),
            cc_recipients=self._people(payload.get("cc_emails")),
            direction="outbound",
            is_unread=False,
            mailbox_state=scheduled.status,
            occurred_at=scheduled.scheduled_at,
            customers=(HubMailboxCustomerLink(id=customer.id, name=customer.name),) if customer is not None else (),
            customer_id=scheduled.customer_id,
            customer_email_id=None,
            preview_html=CustomerCommunicationService._email_preview_document(self._text(payload.get("content"))),
            attachments=tuple(
                CustomerCommunicationAttachment(id=str(attachment.id), filename=attachment.filename)
                for attachment in scheduled.attachments
            ),
            can_load_content=False,
            last_error=scheduled.last_error,
            scheduled_email_id=scheduled.id,
        )

    def download_unassigned_attachment(
        self,
        *,
        email_id: int,
        attachment_id: str,
    ) -> CustomerCommunicationAttachmentDownload:
        if self.scope is not None:
            self.scope.require(f"unassigned-{email_id}")
        email = self.db.get(HubMailboxEmail, email_id)
        if email is None:
            raise ValueError("Die E-Mail wurde nicht gefunden.")
        payload = self._payload(email.encrypted_payload_json)
        attachment = next(
            (item for item in self.communications._email_attachments(payload) if item.id == attachment_id),
            None,
        )
        if attachment is None:
            raise ValueError("Der angeforderte Anhang gehört nicht zu dieser E-Mail.")
        stored = self.db.scalar(
            select(HubMailboxAttachment).where(
                HubMailboxAttachment.email_id == email.id,
                HubMailboxAttachment.source_attachment_id == attachment.id,
            )
        )
        if stored is None:
            raise ValueError("Für diesen Anhang liegt keine lokale Sicherung vor.")
        try:
            content = self.communications.attachment_storage.load(stored.storage_key)
        except EmailAttachmentStorageError as exc:
            raise ValueError("Die lokale Sicherung dieses Anhangs ist nicht lesbar.") from exc
        return CustomerCommunicationAttachmentDownload(
            content=content,
            content_type=stored.content_type,
            filename=attachment.filename,
        )

    @staticmethod
    def _linked_customers(emails: list[CustomerZohoEmail]) -> tuple[HubMailboxCustomerLink, ...]:
        customers_by_id = {
            email.customer.id: email.customer
            for email in emails
            if email.customer is not None
        }
        return tuple(
            HubMailboxCustomerLink(id=customer.id, name=customer.name)
            for customer in sorted(
                customers_by_id.values(),
                key=lambda customer: (customer.name.casefold(), customer.id),
            )
        )

    @staticmethod
    def _matches_folder(message: HubMailboxMessage | HubMailboxListItem, folder: str) -> bool:
        if folder == "planned":
            return message.kind == "scheduled" and message.mailbox_state in _PLANNED_MAILBOX_STATUSES
        if folder == "drafts":
            return message.kind == "draft" and message.mailbox_state == _DRAFT_MAILBOX_STATE
        if folder in _MAILBOX_STATE_BY_FOLDER:
            return message.mailbox_state == _MAILBOX_STATE_BY_FOLDER[folder]
        if folder == "unassigned":
            return message.kind == "unassigned" and message.mailbox_state == _ACTIVE_MAILBOX_STATE
        if folder == "sent":
            return message.mailbox_state == _ACTIVE_MAILBOX_STATE and message.direction == "outbound"
        return message.mailbox_state == _ACTIVE_MAILBOX_STATE and message.direction == "inbound"

    @staticmethod
    def _unassigned_kind(email: HubMailboxEmail) -> str:
        if email.source == _DRAFT_SOURCE and email.mailbox_state == _DRAFT_MAILBOX_STATE:
            return "draft"
        if email.source == _DIRECT_SEND_SOURCE:
            return "direct"
        if email.source in {TASK_EMAIL_REMINDER_SOURCE, MAILBOX_HEALTH_ALERT_SOURCE}:
            return "system"
        return "unassigned"

    @staticmethod
    def _validated_direct_attachments(
        attachments: tuple[CustomerCommunicationAttachmentUpload, ...],
    ) -> tuple[CustomerCommunicationAttachmentUpload, ...]:
        if len(attachments) > 20:
            raise ValueError("Es können höchstens 20 Anhänge pro E-Mail versendet werden.")
        total_bytes = 0
        for attachment in attachments:
            CustomerCommunicationService._required_text(attachment.filename, "Dateiname", maximum=255)
            if not attachment.content:
                raise ValueError("Ein leerer Anhang kann nicht versendet werden.")
            total_bytes += len(attachment.content)
            if total_bytes > 50 * 1024 * 1024:
                raise ValueError("Die Anhänge sind zusammen größer als 50 MB.")
        return attachments

    def _payload(self, encrypted_payload_json: str) -> dict[str, object]:
        try:
            payload = self.cipher.decrypt(encrypted_payload_json)
            decoded = json.loads(payload)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @classmethod
    def _unassigned_content(cls, payload: dict[str, object]) -> str | None:
        for container in cls._payload_containers(payload):
            for key in ("content", "body", "message", "html", "nachricht", "email_content", "mail_content"):
                content = cls._text(container.get(key))
                if content:
                    return content
        return None

    @staticmethod
    def _payload_containers(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
        containers = [payload]
        for key in ("data", "payload", "record", "current_record", "currentrecord", "aufzeichnung"):
            value = payload.get(key)
            if isinstance(value, dict):
                containers.append(value)
                continue
            if not isinstance(value, str):
                continue
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                containers.append(parsed)
        return tuple(containers)

    @staticmethod
    def _text(value: object) -> str | None:
        return CustomerCommunicationService._text(value)

    @staticmethod
    def _people(value: object) -> str | None:
        return CustomerCommunicationService._people_text(value)
