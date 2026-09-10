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

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only, selectinload

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxAttachment, HubMailboxEmail
from app.services.customer_communications import (
    CustomerCommunicationAttachment,
    CustomerCommunicationAttachmentDownload,
    CustomerCommunicationAttachmentUpload,
    CustomerCommunicationService,
)
from app.services.email_attachment_storage import EmailAttachmentStorage, EmailAttachmentStorageError
from app.services.email_compose_images import EmailComposeImageError, EmailComposeImageService
from app.services.email_composer_settings import EmailComposerSettingsService
from app.services.email_html_compiler import EmailHtmlCompiler
from app.services.hub_mailbox_transport import (
    HubMailboxTransportAttachment,
    HubMailboxTransportError,
    HubMailboxTransportInlineImage,
    HubMailboxTransportService,
)


MAILBOX_FOLDERS = frozenset({"inbox", "sent", "drafts", "unassigned", "trash", "spam"})
_ACTIVE_MAILBOX_STATE = "active"
_DRAFT_MAILBOX_STATE = "draft"
_DRAFT_SOURCE = "hub-draft"
_DIRECT_SEND_SOURCE = "hub-direct-send"
TASK_EMAIL_REMINDER_SOURCE = "hub-task-reminder"
MAILBOX_HEALTH_ALERT_SOURCE = "hub-mailbox-health-alert"
_MAILBOX_STATE_BY_FOLDER = {"drafts": _DRAFT_MAILBOX_STATE, "trash": "trash", "spam": "spam"}
_BATCH_ACTIONS = frozenset(
    {"mark_read", "mark_unread", "move_inbox", "move_sent", "move_trash", "move_spam", "restore", "permanently_delete"}
)


@dataclass(frozen=True)
class HubMailboxCustomerLink:
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
    ) -> None:
        self.db = db
        self.cipher = cipher
        self.communications = CustomerCommunicationService(
            db=db,
            cipher=cipher,
            public_base_url=public_base_url,
            attachment_storage=attachment_storage,
        )

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
        """Return lightweight sidebar counts without loading encrypted email payloads."""
        return self._folder_counts()

    def get_folder_view(self, *, folder: str, unread_only: bool, selected_key: str = "") -> HubMailboxView:
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
        ) if selected_item else None
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
                message = self._linked_message(emails)
        elif selected_key.startswith("unassigned-"):
            try:
                email_id = int(selected_key.removeprefix("unassigned-"))
            except ValueError:
                email_id = 0
            email = self.db.get(HubMailboxEmail, email_id)
            if email is not None:
                message = self._unassigned_message(email)

        if message is None or not self._matches_folder(message, folder):
            return None
        if unread_only and not message.is_unread:
            return None
        return message

    def mark_unassigned_read(self, *, email_id: int) -> None:
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
                self.communications.attachment_storage.remove(storage_key)
            return len(selected_emails)

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
                email.mailbox_state = "spam"
            elif action == "restore":
                email.mailbox_state = _ACTIVE_MAILBOX_STATE
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
        recipient_name: str,
        subject: str,
        content: str,
        cc_emails: str,
        template_id: str,
        reply_to_email_id: str,
        forward_from_email_id: str,
    ) -> HubMailboxEmail:
        """Persist the editable fields of an unsent mailbox message in encrypted storage."""
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
        else:
            draft = self.db.get(HubMailboxEmail, draft_id)
            if draft is None or draft.source != _DRAFT_SOURCE or draft.mailbox_state != _DRAFT_MAILBOX_STATE:
                raise ValueError("Der Entwurf wurde nicht gefunden.")

        payload = {
            "subject": subject.strip()[:500],
            "sender": sender_email.strip()[:320],
            "recipient_email": recipient_email.strip()[:320],
            "recipient_key": recipient_key.strip()[:255],
            "recipient_customer_id": recipient_customer_id,
            "recipient_name": recipient_name.strip()[:255],
            "content": content[:500_000],
            "cc_emails": cc_emails.strip()[:2_000],
            "template_id": template_id.strip()[:255],
            "reply_to_email_id": reply_to_email_id.strip()[:64],
            "forward_from_email_id": forward_from_email_id.strip()[:64],
        }
        draft.direction = "outbound"
        draft.is_unread = False
        draft.mailbox_state = _DRAFT_MAILBOX_STATE
        draft.encrypted_payload_json = self.cipher.encrypt(json.dumps(payload, ensure_ascii=False))
        draft.received_at = datetime.now(UTC)
        draft.last_error = None
        self.db.flush()
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

        transport = HubMailboxTransportService(db=self.db, cipher=self.cipher)
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
        self.db.flush()
        return email

    def get_draft_compose_context(self, *, draft_id: int) -> dict[str, object]:
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
            "subject": self._text(payload.get("subject")) or "",
            "content": self._text(payload.get("content")) or "",
            "cc_emails": self._text(payload.get("cc_emails")) or "",
            "template_id": self._text(payload.get("template_id")) or "",
            "reply_to_email_id": self._text(payload.get("reply_to_email_id")) or "",
            "forward_from_email_id": self._text(payload.get("forward_from_email_id")) or "",
        }

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
            signature_html = EmailComposerSettingsService(db=self.db).get_runtime_settings().signature_html
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

    def discard_draft(self, *, draft_id: int) -> bool:
        draft = self.db.get(HubMailboxEmail, draft_id)
        if draft is None or draft.source != _DRAFT_SOURCE or draft.mailbox_state != _DRAFT_MAILBOX_STATE:
            return False
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
        ]

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
        return HubMailboxListItem(
            key=f"unassigned-{email.id}",
            kind=kind,
            subject=self._text(payload.get("betreff")) or self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._people(payload.get("absender") or payload.get("sender") or payload.get("from")),
            recipients=self._people(payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient") or payload.get("to")),
            direction=email.direction,
            is_unread=email.is_unread,
            mailbox_state=email.mailbox_state,
            occurred_at=email.received_at,
            customers=(),
        )

    def _folder_counts(self) -> dict[str, int]:
        """Build sidebar counts from message identifiers, never encrypted bodies."""
        linked_keys = {
            (mailbox_state, direction, message_id or f"local-{email_id}")
            for mailbox_state, direction, message_id, email_id in self.db.execute(
                select(
                    CustomerZohoEmail.mailbox_state,
                    CustomerZohoEmail.direction,
                    CustomerZohoEmail.zoho_message_id,
                    CustomerZohoEmail.id,
                )
            )
        }
        unassigned_rows = list(self.db.execute(
            select(HubMailboxEmail.mailbox_state, HubMailboxEmail.direction, HubMailboxEmail.source, HubMailboxEmail.id)
        ))
        active_unassigned_rows = [
            (direction, source, email_id)
            for mailbox_state, direction, source, email_id in unassigned_rows
            if mailbox_state == _ACTIVE_MAILBOX_STATE
        ]
        return {
            "inbox": sum(
                mailbox_state == _ACTIVE_MAILBOX_STATE and direction == "inbound"
                for mailbox_state, direction, _ in linked_keys
            ) + sum(direction == "inbound" for direction, _source, _ in active_unassigned_rows),
            "sent": sum(
                mailbox_state == _ACTIVE_MAILBOX_STATE and direction == "outbound"
                for mailbox_state, direction, _ in linked_keys
            ) + sum(direction == "outbound" for direction, _source, _ in active_unassigned_rows),
            "unassigned": sum(direction == "inbound" for direction, _source, _ in active_unassigned_rows),
            "trash": sum(mailbox_state == "trash" for mailbox_state, _, _ in linked_keys)
            + sum(mailbox_state == "trash" for mailbox_state, _, _source, _ in unassigned_rows),
            "spam": sum(mailbox_state == "spam" for mailbox_state, _, _ in linked_keys)
            + sum(mailbox_state == "spam" for mailbox_state, _, _source, _ in unassigned_rows),
            "drafts": sum(
                mailbox_state == _DRAFT_MAILBOX_STATE and source == _DRAFT_SOURCE
                for mailbox_state, _direction, source, _email_id in unassigned_rows
            ),
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
        )

    def _unassigned_message(self, email: HubMailboxEmail) -> HubMailboxMessage:
        payload = self._payload(email.encrypted_payload_json)
        content = self._unassigned_content(payload)
        kind = self._unassigned_kind(email)
        return HubMailboxMessage(
            key=f"unassigned-{email.id}",
            kind=kind,
            subject=self._text(payload.get("betreff")) or self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._people(payload.get("absender") or payload.get("sender") or payload.get("from")),
            recipients=self._people(payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient") or payload.get("to")),
            cc_recipients=self._people(payload.get("cc")),
            direction=email.direction,
            is_unread=email.is_unread,
            mailbox_state=email.mailbox_state,
            occurred_at=email.received_at,
            customers=(),
            customer_id=None,
            customer_email_id=email.id,
            # Deluge can send an unknown email body directly; render it only inside the sandboxed preview.
            preview_html=CustomerCommunicationService._email_preview_document(content),
            attachments=self.communications._email_attachments(payload),
            can_load_content=False,
            last_error=email.last_error,
        )

    def download_unassigned_attachment(
        self,
        *,
        email_id: int,
        attachment_id: str,
    ) -> CustomerCommunicationAttachmentDownload:
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
