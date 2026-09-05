"""Encrypted per-customer communication history backed by Zoho CRM."""

from __future__ import annotations

import json
import re
import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr
from html import escape, unescape
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail, CustomerZohoEmailImage, CustomerZohoNote
from app.models.customer_contact import CustomerContact
from app.models.zoho_email_template import ZohoEmailTemplate
from app.services.zoho_crm import ZOHO_ACCOUNT_MODULE, ZOHO_CONTACT_MODULE, ZohoCrmError, ZohoCrmService


_EMAIL_IMAGE_CACHE_TTL = timedelta(days=30)
_MAX_EXTERNAL_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_EMAIL_IMAGE_CACHE_BYTES = 250 * 1024 * 1024
_MAX_OUTBOUND_ATTACHMENT_TOTAL_BYTES = 10 * 1024 * 1024
_MAX_OUTBOUND_ATTACHMENT_COUNT = 10
_EXTERNAL_IMAGE_TIMEOUT_SECONDS = 15
_ALLOWED_IMAGE_CONTENT_TYPES = frozenset({"image/avif", "image/gif", "image/jpeg", "image/png", "image/webp"})
_IMAGE_SRC_PATTERN = re.compile(
    r"(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)(?P<quote>['\"])(?P<source>.*?)(?P=quote)",
    flags=re.IGNORECASE,
)
_ZOHO_INLINE_IMAGE_SOURCE_PATTERN = re.compile(r"^crm\\img_id:(?P<image_id>[A-Za-z0-9_-]{1,255})$", flags=re.IGNORECASE)
_COMPOSER_COLOR_PATTERN = re.compile(r"^#[0-9a-f]{3}(?:[0-9a-f]{3})?$", flags=re.IGNORECASE)
_COMPOSER_TEXT_ALIGNMENTS = frozenset({"left", "center", "right", "justify"})
_COMPOSER_ALLOWED_TAGS = frozenset({
    "a", "b", "blockquote", "br", "div", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
    "li", "ol", "p", "pre", "s", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
})
_COMPOSER_VOID_TAGS = frozenset({"br", "hr", "img"})
_COMPOSER_STYLE_PROPERTIES = frozenset({
    "background", "background-color", "border", "border-bottom", "border-collapse", "border-color", "border-radius", "border-spacing",
    "border-style", "border-width", "color", "display", "font-family", "font-size", "font-style", "font-weight", "height", "line-height",
    "margin", "margin-bottom", "margin-left", "margin-right", "margin-top", "max-width", "min-width", "padding", "padding-bottom", "padding-left",
    "padding-right", "padding-top", "text-align", "text-decoration", "vertical-align", "width",
})
_COMPOSER_STYLE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9#%(),.\s/+_-]+$")


class _EmailComposerHtmlSanitizer(HTMLParser):
    """Keep safe rich-text and layout markup emitted by the Hub or Zoho templates."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open_tags: list[str] = []
        self._ignored_tag_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in {"script", "style", "template"}:
            self._ignored_tag_depth += 1
            return
        if self._ignored_tag_depth:
            return
        if normalized_tag not in _COMPOSER_ALLOWED_TAGS:
            return
        rendered_attrs = self._render_attributes(normalized_tag, attrs)
        if normalized_tag == "a" and not rendered_attrs:
            return
        self._parts.append(f"<{normalized_tag}{rendered_attrs}>")
        if normalized_tag not in _COMPOSER_VOID_TAGS:
            self._open_tags.append(normalized_tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _COMPOSER_VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in {"script", "style", "template"}:
            self._ignored_tag_depth = max(0, self._ignored_tag_depth - 1)
            return
        if self._ignored_tag_depth:
            return
        if normalized_tag not in self._open_tags:
            return
        while self._open_tags:
            opened_tag = self._open_tags.pop()
            self._parts.append(f"</{opened_tag}>")
            if opened_tag == normalized_tag:
                return

    def handle_data(self, data: str) -> None:
        if not self._ignored_tag_depth:
            self._parts.append(escape(data))

    def content(self) -> str:
        while self._open_tags:
            self._parts.append(f"</{self._open_tags.pop()}>")
        return "".join(self._parts)

    @staticmethod
    def _render_attributes(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        values = {name.casefold(): (value or "").strip() for name, value in attrs}
        if tag == "a":
            href = values.get("href", "")
            scheme = urlsplit(href).scheme.casefold()
            if scheme in {"http", "https", "mailto"}:
                return f' href="{escape(href, quote=True)}"'
            return ""
        if tag == "img":
            source = values.get("src", "")
            scheme = urlsplit(source).scheme.casefold()
            if scheme not in {"http", "https", "data"}:
                return ""
            attributes = [f' src="{escape(source, quote=True)}"']
            for attribute in ("alt", "title", "width", "height"):
                value = values.get(attribute, "")
                if value:
                    attributes.append(f' {attribute}="{escape(value, quote=True)}"')
            return "".join(attributes)
        if tag == "font":
            attributes: list[str] = []
            color = values.get("color", "")
            if _COMPOSER_COLOR_PATTERN.fullmatch(color):
                attributes.append(f' color="{escape(color, quote=True)}"')
            size = values.get("size", "")
            if size in {"1", "2", "3", "4", "5", "6", "7"}:
                attributes.append(f' size="{size}"')
            return "".join(attributes)
        attributes: list[str] = []
        style = _EmailComposerHtmlSanitizer._safe_style(values.get("style", ""))
        if style:
            attributes.append(f' style="{escape(style, quote=True)}"')
        for attribute in ("align", "valign", "width", "height", "border", "cellpadding", "cellspacing", "colspan", "rowspan"):
            value = values.get(attribute, "")
            if value and re.fullmatch(r"[A-Za-z0-9.% -]{1,40}", value):
                attributes.append(f' {attribute}="{escape(value, quote=True)}"')
        return "".join(attributes)

    @staticmethod
    def _safe_style(style: str) -> str:
        declarations: list[str] = []
        for declaration in style.split(";"):
            property_name, separator, value = declaration.partition(":")
            normalized_property = property_name.strip().casefold()
            normalized_value = value.strip()
            if not separator or normalized_property not in _COMPOSER_STYLE_PROPERTIES:
                continue
            if normalized_property == "text-align" and normalized_value.casefold() not in _COMPOSER_TEXT_ALIGNMENTS:
                continue
            if not normalized_value or not _COMPOSER_STYLE_VALUE_PATTERN.fullmatch(normalized_value):
                continue
            declarations.append(f"{normalized_property}: {normalized_value}")
        return "; ".join(declarations)


class CustomerCommunicationImageError(Exception):
    """Raised when an external image cannot safely be made available for a preview."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class CustomerCommunicationRecipient:
    key: str
    name: str
    email: str


@dataclass(frozen=True)
class CustomerCommunicationRecipientSearchMatch:
    """A locally stored recipient together with its Zoho Account context."""

    customer_id: int
    customer_name: str
    recipient: CustomerCommunicationRecipient


@dataclass(frozen=True)
class CustomerCommunicationSender:
    name: str
    email: str


@dataclass(frozen=True)
class CustomerCommunicationEmailTemplate:
    id: str
    name: str
    subject: str
    module: str
    category: str


@dataclass(frozen=True)
class CustomerCommunicationEmailTemplateDetail:
    id: str
    name: str
    subject: str
    content: str
    unresolved_placeholders: tuple[str, ...]


@dataclass(frozen=True)
class CustomerCommunicationEmailTemplateSyncResult:
    created: int
    updated: int
    archived: int


@dataclass(frozen=True)
class CustomerCommunicationAttachment:
    id: str
    filename: str


@dataclass(frozen=True)
class CustomerCommunicationAttachmentDownload:
    content: bytes
    content_type: str
    filename: str


@dataclass(frozen=True)
class CustomerCommunicationAttachmentUpload:
    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class CustomerCommunicationCachedImage:
    content: bytes
    content_type: str


@dataclass(frozen=True)
class CustomerCommunicationImageSource:
    kind: str
    value: str


@dataclass(frozen=True)
class CustomerCommunicationNoteView:
    id: int
    title: str
    content: str
    source: str
    sync_status: str
    author: str | None
    occurred_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class CustomerCommunicationEmailView:
    id: int
    subject: str
    sender: str | None
    recipients: str | None
    direction: str
    is_unread: bool
    source: str
    sync_status: str
    occurred_at: datetime | None
    preview_html: str | None
    attachments: tuple[CustomerCommunicationAttachment, ...]
    can_load_content: bool
    last_error: str | None


@dataclass(frozen=True)
class CustomerCommunicationEmailReply:
    email_id: int
    recipient_key: str
    recipient_name: str
    recipient_email: str
    subject: str
    reply_all_cc_emails: tuple[str, ...]


@dataclass(frozen=True)
class CustomerCommunicationEmailForward:
    email_id: int
    subject: str
    content: str


@dataclass(frozen=True)
class CustomerCommunicationView:
    notes: tuple[CustomerCommunicationNoteView, ...]
    emails: tuple[CustomerCommunicationEmailView, ...]
    recipients: tuple[CustomerCommunicationRecipient, ...]
    last_synced_at: datetime | None


@dataclass(frozen=True)
class CustomerCommunicationSyncResult:
    notes: int
    emails: int


@dataclass(frozen=True)
class CustomerCommunicationEmailHeaderSyncResult:
    emails: int
    loaded_contents: int = 0


@dataclass(frozen=True)
class CustomerCommunicationActionResult:
    success: bool
    message: str


class CustomerCommunicationService:
    """Keeps customer conversations available in the Hub without exposing plaintext at rest."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        public_base_url: str,
        zoho_service: ZohoCrmService | None = None,
    ):
        self.db = db
        self.cipher = cipher
        self.zoho_service = zoho_service or ZohoCrmService(
            db=db,
            cipher=cipher,
            public_base_url=public_base_url,
        )

    def get_view(self, *, customer_id: int) -> CustomerCommunicationView:
        customer = self._require_customer(customer_id)
        notes = [self._note_view(note) for note in self.db.scalars(
            select(CustomerZohoNote)
            .where(CustomerZohoNote.customer_id == customer.id)
            .order_by(CustomerZohoNote.zoho_modified_at.desc(), CustomerZohoNote.created_at.desc(), CustomerZohoNote.id.desc())
        ).all()]
        emails = [self._email_view(email) for email in self.db.scalars(
            select(CustomerZohoEmail)
            .where(CustomerZohoEmail.customer_id == customer.id)
            .order_by(CustomerZohoEmail.zoho_sent_at.desc(), CustomerZohoEmail.created_at.desc(), CustomerZohoEmail.id.desc())
        ).all()]
        last_synced_at = max(
            (timestamp for timestamp in (
                *(note.occurred_at for note in notes),
                *(email.occurred_at for email in emails),
            ) if timestamp is not None),
            default=None,
        )
        return CustomerCommunicationView(
            notes=tuple(notes),
            emails=tuple(emails),
            recipients=tuple(self._recipients_for_customer(customer)),
            last_synced_at=last_synced_at,
        )

    def list_senders(self) -> tuple[CustomerCommunicationSender, ...]:
        """Read the current Zoho-approved sender list immediately before composing."""
        senders: list[CustomerCommunicationSender] = []
        seen: set[str] = set()
        for record in self.zoho_service.list_allowed_from_addresses():
            email = self._text(record.get("email"))
            if not email or "@" not in email or email.casefold() in seen:
                continue
            seen.add(email.casefold())
            senders.append(
                CustomerCommunicationSender(
                    name=self._text(record.get("user_name")) or email,
                    email=email.casefold(),
                )
            )
        if not senders:
            raise ZohoCrmError("Zoho CRM has no verified sender address for this connection.")
        return tuple(senders)

    def list_recipients(self, *, customer_id: int) -> tuple[CustomerCommunicationRecipient, ...]:
        """List only the current customer's valid recipient addresses for the mailbox composer."""
        customer = self._require_zoho_customer(customer_id)
        return tuple(self._recipients_for_customer(customer))

    def search_recipients(self, *, query: str, limit: int = 12) -> tuple[CustomerCommunicationRecipientSearchMatch, ...]:
        """Find known recipient addresses without sending a new request to Zoho."""
        normalized_query = " ".join(query.casefold().split())
        if len(normalized_query) < 2:
            return ()
        tokens = tuple(token for token in re.split(r"\s+", normalized_query) if token)
        matches: list[CustomerCommunicationRecipientSearchMatch] = []
        for customer in self.db.scalars(
            select(Customer)
            .where(Customer.is_visible.is_(True), Customer.zoho_id.is_not(None))
            .order_by(Customer.name.asc(), Customer.id.asc())
        ).all():
            for recipient in self._recipients_for_customer(customer):
                searchable = " ".join((recipient.name, recipient.email, customer.name)).casefold()
                if not all(token in searchable for token in tokens):
                    continue
                matches.append(
                    CustomerCommunicationRecipientSearchMatch(
                        customer_id=customer.id,
                        customer_name=customer.name,
                        recipient=recipient,
                    )
                )

        def sort_key(match: CustomerCommunicationRecipientSearchMatch) -> tuple[int, int, str, str, int]:
            recipient = match.recipient
            return (
                0 if recipient.email.startswith(normalized_query) else 1,
                0 if recipient.name.casefold().startswith(normalized_query) else 1,
                match.customer_name.casefold(),
                recipient.name.casefold(),
                match.customer_id,
            )

        return tuple(sorted(matches, key=sort_key)[:max(1, min(limit, 30))])

    def list_email_templates(self) -> tuple[CustomerCommunicationEmailTemplate, ...]:
        """List the local, encrypted Zoho templates without querying Zoho."""
        templates: list[CustomerCommunicationEmailTemplate] = []
        for template in self.db.scalars(
            select(ZohoEmailTemplate)
            .where(ZohoEmailTemplate.is_active.is_(True))
            .order_by(ZohoEmailTemplate.id.asc())
        ).all():
            payload = self._payload(template.encrypted_payload_json)
            template_id = template.zoho_template_id
            name = self._text(payload.get("name"))
            if not template_id or not name:
                continue
            category = self._text(payload.get("folder_name")) or template.module
            templates.append(
                CustomerCommunicationEmailTemplate(
                    id=template_id,
                    name=name,
                    subject=self._text(payload.get("subject")) or "",
                    module=template.module,
                    category=category,
                )
            )
        return tuple(sorted(templates, key=lambda item: (item.module.casefold(), item.name.casefold())))

    def get_email_template(
        self,
        *,
        customer_id: int,
        template_id: str,
        recipient_key: str = "",
    ) -> CustomerCommunicationEmailTemplateDetail:
        customer = self._require_zoho_customer(customer_id)
        template = self._stored_email_template(template_id)
        payload = self._payload(template.encrypted_payload_json)
        name = self._required_text(self._text(payload.get("name")) or "", "Vorlagenname", maximum=255)
        recipient = next((item for item in self._recipients_for_customer(customer) if item.key == recipient_key), None)
        if recipient is None:
            recipient = next(iter(self._recipients_for_customer(customer)), None)
        context = self._email_template_context(customer=customer, recipient=recipient)
        subject, subject_placeholders = self._resolve_template_placeholders(
            self._text(payload.get("subject")) or "",
            context=context,
            html=False,
        )
        content, content_placeholders = self._resolve_template_placeholders(
            self._sanitized_email_content(self._text(payload.get("content")) or ""),
            context=context,
            html=True,
        )
        return CustomerCommunicationEmailTemplateDetail(
            id=template.zoho_template_id,
            name=name,
            subject=subject,
            content=content,
            unresolved_placeholders=tuple(sorted({*subject_placeholders, *content_placeholders})),
        )

    def sync_email_templates(self) -> CustomerCommunicationEmailTemplateSyncResult:
        """Synchronize all Zoho email-template modules once for local composition."""
        synced_at = datetime.now(UTC)
        existing = {
            template.zoho_template_id: template
            for template in self.db.scalars(select(ZohoEmailTemplate)).all()
        }
        synchronized_ids: set[str] = set()
        created = 0
        updated = 0
        for summary in self.zoho_service.list_email_templates():
            template_id = self._required_template_id(self._text(summary.get("id")) or "")
            summary_module = self._template_module_from_record(summary)
            record = self.zoho_service.get_email_template(template_id=template_id)
            name = self._required_text(self._text(record.get("name")) or "", "Vorlagenname", maximum=255)
            subject = self._text(record.get("subject")) or ""
            content = self._sanitized_email_content(self._text(record.get("content")) or "")
            folder = record.get("folder") if isinstance(record.get("folder"), dict) else {}
            payload = {
                "name": name,
                "subject": subject,
                "content": content,
                "category": self._text(record.get("category")) or "",
                "folder_id": self._text(folder.get("id")) or "",
                "folder_name": self._text(folder.get("name")) or "",
            }
            template_module = self._template_module_from_record(record, fallback=summary_module)
            modified_at = self._datetime(record.get("modified_time") or record.get("Modified_Time"))
            synchronized_ids.add(template_id)
            template = existing.get(template_id)
            if template is None:
                self.db.add(
                    ZohoEmailTemplate(
                        zoho_template_id=template_id,
                        module=template_module,
                        encrypted_payload_json=self._encrypt_payload(payload),
                        zoho_modified_at=modified_at,
                        zoho_synced_at=synced_at,
                        is_active=True,
                    )
                )
                created += 1
                continue
            previous_payload = self._payload(template.encrypted_payload_json)
            if (
                previous_payload != payload
                or template.module != template_module
                or template.zoho_modified_at != modified_at
                or not template.is_active
            ):
                updated += 1
            template.module = template_module
            template.encrypted_payload_json = self._encrypt_payload(payload)
            template.zoho_modified_at = modified_at
            template.zoho_synced_at = synced_at
            template.is_active = True

        archived = 0
        for template_id, template in existing.items():
            if template.is_active and template_id not in synchronized_ids:
                template.is_active = False
                archived += 1
        self.db.flush()
        return CustomerCommunicationEmailTemplateSyncResult(created=created, updated=updated, archived=archived)

    def sync_customer(
        self,
        *,
        customer_id: int,
        mark_new_emails_unread: bool = False,
        load_new_inbound_content: bool = False,
    ) -> CustomerCommunicationSyncResult:
        customer = self._require_zoho_customer(customer_id)
        synced_at = datetime.now(UTC)
        note_count = 0
        known_notes = {
            note.zoho_note_id: note
            for note in self.db.scalars(
                select(CustomerZohoNote).where(CustomerZohoNote.zoho_note_id.is_not(None))
            ).all()
            if note.zoho_note_id
        }
        for record in self.zoho_service.list_account_notes(customer.zoho_id):
            if self._upsert_zoho_note(
                customer=customer,
                record=record,
                synced_at=synced_at,
                known_notes=known_notes,
            ):
                note_count += 1

        email_result = self._sync_customer_email_headers(
            customer=customer,
            synced_at=synced_at,
            mark_new_emails_unread=mark_new_emails_unread,
            load_new_inbound_content=load_new_inbound_content,
        )
        return CustomerCommunicationSyncResult(notes=note_count, emails=email_result.emails)

    def sync_customer_email_headers(
        self,
        *,
        customer_id: int,
        mark_new_emails_unread: bool = False,
        load_new_inbound_content: bool = False,
    ) -> CustomerCommunicationEmailHeaderSyncResult:
        """Synchronize only the Zoho email headers, without reloading notes."""
        customer = self._require_zoho_customer(customer_id)
        return self._sync_customer_email_headers(
            customer=customer,
            synced_at=datetime.now(UTC),
            mark_new_emails_unread=mark_new_emails_unread,
            load_new_inbound_content=load_new_inbound_content,
        )

    def _sync_customer_email_headers(
        self,
        *,
        customer: Customer,
        synced_at: datetime,
        mark_new_emails_unread: bool,
        load_new_inbound_content: bool,
    ) -> CustomerCommunicationEmailHeaderSyncResult:
        email_count = 0
        known_emails = {
            (email.customer_id, email.zoho_message_id): email
            for email in self.db.scalars(
                select(CustomerZohoEmail).where(
                    CustomerZohoEmail.customer_id == customer.id,
                    CustomerZohoEmail.zoho_message_id.is_not(None),
                )
            ).all()
            if email.zoho_message_id
        }
        targets = [(ZOHO_ACCOUNT_MODULE, customer.zoho_id)]
        targets.extend(
            (ZOHO_CONTACT_MODULE, contact.zoho_id)
            for contact in self.db.scalars(
                select(CustomerContact).where(CustomerContact.customer_id == customer.id)
            ).all()
            if contact.zoho_id
        )
        for module, record_id in targets:
            for record in self.zoho_service.list_record_email_headers(module, record_id):
                created_email = self._upsert_zoho_email(
                    customer=customer,
                    record=record,
                    module=module,
                    record_id=record_id,
                    synced_at=synced_at,
                    known_emails=known_emails,
                    mark_new_emails_unread=mark_new_emails_unread,
                )
                if created_email is not None:
                    email_count += 1

        self.db.flush()
        duplicate_count = self._deduplicate_customer_emails(customer_id=customer.id)
        loaded_contents = 0
        if load_new_inbound_content:
            # Webhooks set only newly discovered inbound messages to unread. Looking them up again
            # after deduplication also handles the Account/Contact copies Zoho may return together.
            unread_emails = self.db.scalars(
                select(CustomerZohoEmail).where(
                    CustomerZohoEmail.customer_id == customer.id,
                    CustomerZohoEmail.direction == "inbound",
                    CustomerZohoEmail.is_unread.is_(True),
                )
            ).all()
            for email in unread_emails:
                if self.has_loaded_email_content(email):
                    continue
                try:
                    self._load_email_content_for_email(email, mark_as_read=False)
                    loaded_contents += 1
                except (ValueError, ZohoCrmError) as exc:
                    # Keep the unread header visible even when Zoho cannot supply the body yet.
                    email.last_error = str(exc)[:1000]
            self.db.flush()
        return CustomerCommunicationEmailHeaderSyncResult(
            emails=max(0, email_count - duplicate_count),
            loaded_contents=loaded_contents,
        )

    def create_note(
        self,
        *,
        customer_id: int,
        actor: str,
        title: str,
        content: str,
    ) -> CustomerCommunicationActionResult:
        customer = self._require_zoho_customer(customer_id)
        normalized_content = self._required_text(content, "Notiz", maximum=30_000)
        normalized_title = (
            self._required_text(title, "Titel", maximum=255)
            if title.strip()
            else self._note_title_from_content(normalized_content)
        )
        now = datetime.now(UTC)
        note = CustomerZohoNote(
            customer=customer,
            source="hub",
            sync_status="pending",
            encrypted_payload_json=self._encrypt_payload({"title": normalized_title, "content": normalized_content}),
            created_by_username=actor[:64],
            zoho_created_at=now,
        )
        self.db.add(note)
        self.db.flush()
        try:
            created = self.zoho_service.create_account_note(
                account_id=customer.zoho_id,
                title=normalized_title,
                content=normalized_content,
            )
        except ZohoCrmError as exc:
            note.sync_status = "failed"
            note.last_error = str(exc)[:1000]
            self.db.flush()
            return CustomerCommunicationActionResult(False, "Notiz wurde im Hub gespeichert, konnte aber noch nicht an Zoho gesendet werden.")

        note.zoho_note_id = self._text(created.get("id"))
        note.zoho_created_at = self._datetime(created.get("Created_Time")) or now
        note.zoho_modified_at = self._datetime(created.get("Modified_Time")) or note.zoho_created_at
        note.zoho_synced_at = now
        note.sync_status = "synced"
        note.last_error = None
        self.db.flush()
        return CustomerCommunicationActionResult(True, "Notiz wurde an Zoho CRM übertragen.")

    def send_email(
        self,
        *,
        customer_id: int,
        actor: str,
        sender_email: str,
        recipient_key: str,
        subject: str,
        content: str,
        confirmed: bool,
        template_id: str = "",
        reply_to_email_id: int | None = None,
        cc_emails: str = "",
        forward_from_email_id: int | None = None,
        attachments: tuple[CustomerCommunicationAttachmentUpload, ...] = (),
    ) -> CustomerCommunicationActionResult:
        if not confirmed:
            raise ValueError("Bestätige bitte den Versand über Zoho CRM.")
        customer = self._require_zoho_customer(customer_id)
        sender = next(
            (item for item in self.list_senders() if item.email.casefold() == sender_email.strip().casefold()),
            None,
        )
        if sender is None:
            raise ValueError("Wähle eine aktuell von Zoho erlaubte Absenderadresse aus.")
        recipient = next((item for item in self._recipients_for_customer(customer) if item.key == recipient_key), None)
        if recipient is None:
            raise ValueError("Wähle eine aktuelle E-Mail-Adresse dieses Kunden oder Kontakts aus.")
        if reply_to_email_id is not None and forward_from_email_id is not None:
            raise ValueError("Eine E-Mail kann nicht gleichzeitig Antwort und Weiterleitung sein.")
        cc_recipients = self._cc_recipients(
            cc_emails,
            excluded_emails={sender.email, recipient.email},
        )
        reply_to_message_id: str | None = None
        reply_to_owner_id: str | None = None
        if reply_to_email_id is not None:
            reply = self.get_email_reply(customer_id=customer.id, email_id=reply_to_email_id)
            if reply.recipient_key != recipient.key:
                raise ValueError("Eine Antwort muss an den Absender der ursprünglichen E-Mail gesendet werden.")
            parent = self._require_customer_email(customer_id=customer.id, email_id=reply_to_email_id)
            reply_to_message_id = parent.zoho_message_id
            reply_to_owner_id = self._email_owner_id(self._payload(parent.encrypted_payload_json))
        normalized_subject = self._required_text(subject, "Betreff", maximum=500)
        normalized_content = self._sanitized_email_content(content)
        normalized_template_id = self._required_template_id(template_id) if template_id.strip() else None
        template_name = ""
        if normalized_template_id:
            template = self._stored_email_template(normalized_template_id)
            template_name = self._text(self._payload(template.encrypted_payload_json).get("name")) or ""
        attachment_ids = self._outbound_email_attachment_ids(
            customer_id=customer.id,
            forward_from_email_id=forward_from_email_id,
            attachments=attachments,
        )
        now = datetime.now(UTC)
        outbound_payload = {
            "subject": normalized_subject,
            "content": normalized_content,
            "from": {"name": sender.name, "email": sender.email},
            "to": {"name": recipient.name, "email": recipient.email},
            "cc": [{"name": name, "email": email} for name, email in cc_recipients],
            "sent_time": now.isoformat(),
            "template": {"id": normalized_template_id, "name": template_name} if normalized_template_id else None,
            "in_reply_to": {
                "email_id": reply_to_email_id,
                "message_id": reply_to_message_id,
            } if reply_to_message_id else None,
            "forwarded_from_email_id": forward_from_email_id,
        }
        email = CustomerZohoEmail(
            customer=customer,
            source="hub",
            direction="outbound",
            is_unread=False,
            sync_status="pending",
            encrypted_payload_json=self._encrypt_payload(outbound_payload),
            encrypted_header_json=self._encrypt_email_list_header(outbound_payload),
            created_by_username=actor[:64],
            zoho_module=ZOHO_ACCOUNT_MODULE,
            zoho_record_id=customer.zoho_id,
            zoho_sent_at=now,
        )
        self.db.add(email)
        self.db.flush()
        try:
            sent = self.zoho_service.send_account_email(
                account_id=customer.zoho_id,
                sender_name=sender.name,
                sender_email=sender.email,
                recipient_name=recipient.name,
                recipient_email=recipient.email,
                subject=normalized_subject,
                content=normalized_content,
                reply_to_message_id=reply_to_message_id,
                reply_to_owner_id=reply_to_owner_id,
                cc_recipients=cc_recipients,
                attachment_ids=attachment_ids,
            )
        except ZohoCrmError as exc:
            email.sync_status = "failed"
            email.last_error = str(exc)[:1000]
            self.db.flush()
            return CustomerCommunicationActionResult(False, "E-Mail wurde nicht versendet. Der Entwurf bleibt verschlüsselt im Hub gespeichert.")

        email.zoho_message_id = self._text(sent.get("message_id")) or self._text(sent.get("id"))
        email.sync_status = "sent"
        email.zoho_synced_at = now
        email.last_error = None
        self.db.flush()
        return CustomerCommunicationActionResult(True, "E-Mail wurde über Zoho CRM versendet.")

    def get_email_reply(self, *, customer_id: int, email_id: int) -> CustomerCommunicationEmailReply:
        """Build a safe reply context only for a known inbound Zoho email."""
        email = self._require_customer_email(customer_id=customer_id, email_id=email_id)
        if email.direction != "inbound":
            raise ValueError("Nur auf eingegangene E-Mails kann geantwortet werden.")
        if not email.zoho_message_id:
            raise ValueError("Für diese E-Mail fehlt die Zoho-Nachrichten-ID.")
        customer = self._require_customer(customer_id)
        payload = self._payload(email.encrypted_payload_json)
        sender_addresses = self._email_addresses(payload.get("from"))
        if len(sender_addresses) != 1:
            raise ValueError("Der Absender dieser E-Mail ist nicht eindeutig.")
        sender_email = sender_addresses[0]
        recipient = next(
            (item for item in self._recipients_for_customer(customer) if item.email == sender_email),
            None,
        )
        if recipient is None:
            raise ValueError("Der Absender ist keinem aktuellen Kundenkontakt zugeordnet.")
        subject = self._text(payload.get("subject")) or "Ohne Betreff"
        if not re.match(r"^\s*re\s*:", subject, flags=re.IGNORECASE):
            subject = f"Re: {subject}"
        return CustomerCommunicationEmailReply(
            email_id=email.id,
            recipient_key=recipient.key,
            recipient_name=recipient.name,
            recipient_email=recipient.email,
            subject=subject[:500],
            reply_all_cc_emails=tuple(
                sorted(
                    (
                        set(self._email_addresses(payload.get("to")))
                        | set(self._email_addresses(payload.get("cc")))
                    )
                    - {sender_email}
                )
            ),
        )

    def get_email_forward(self, *, customer_id: int, email_id: int) -> CustomerCommunicationEmailForward:
        """Prepare an editable forwarded copy, fetching its body only when needed."""
        email = self._require_customer_email(customer_id=customer_id, email_id=email_id)
        payload = self._payload(email.encrypted_payload_json)
        original_content = self._text(payload.get("content"))
        if original_content is None:
            self._load_email_content_for_email(email, mark_as_read=False)
            payload = self._payload(email.encrypted_payload_json)
            original_content = self._text(payload.get("content"))
        if original_content is None:
            raise ZohoCrmError("Zoho hat die E-Mail ohne Nachrichtentext geliefert.")
        subject = self._text(payload.get("subject")) or "Ohne Betreff"
        if not re.match(r"^\s*fwd\s*:", subject, flags=re.IGNORECASE):
            subject = f"Fwd: {subject}"
        original_sender = self._people_text(payload.get("from")) or "Unbekannt"
        original_recipients = self._people_text(payload.get("to")) or "Unbekannt"
        original_time = email.zoho_sent_at or email.created_at
        metadata = (
            "<hr><p><strong>Weitergeleitete Nachricht</strong><br>"
            f"Von: {escape(original_sender)}<br>"
            f"An: {escape(original_recipients)}<br>"
            f"Datum: {escape(original_time.isoformat())}<br>"
            f"Betreff: {escape(self._text(payload.get('subject')) or 'Ohne Betreff')}</p>"
        )
        content = f"<p><br></p>{metadata}{self._sanitized_email_content(original_content)}"
        if len(content) > 50_000:
            raise ValueError("Die ursprüngliche E-Mail ist zu groß, um sie vollständig weiterzuleiten.")
        return CustomerCommunicationEmailForward(email_id=email.id, subject=subject[:500], content=content)

    def _outbound_email_attachment_ids(
        self,
        *,
        customer_id: int,
        forward_from_email_id: int | None,
        attachments: tuple[CustomerCommunicationAttachmentUpload, ...],
    ) -> tuple[str, ...]:
        uploads = list(attachments)
        if forward_from_email_id is not None:
            email = self._require_customer_email(customer_id=customer_id, email_id=forward_from_email_id)
            for attachment in self._email_attachments(self._payload(email.encrypted_payload_json)):
                downloaded = self.download_email_attachment(
                    customer_id=customer_id,
                    email_id=email.id,
                    attachment_id=attachment.id,
                )
                uploads.append(
                    CustomerCommunicationAttachmentUpload(
                        filename=downloaded.filename,
                        content=downloaded.content,
                        content_type=downloaded.content_type,
                    )
                )
        if len(uploads) > _MAX_OUTBOUND_ATTACHMENT_COUNT:
            raise ValueError("Zoho erlaubt höchstens zehn Anhänge pro E-Mail.")
        total_bytes = 0
        uploaded_ids: list[str] = []
        for attachment in uploads:
            filename = self._required_text(attachment.filename, "Dateiname", maximum=255)
            if not attachment.content:
                raise ValueError("Ein leerer Anhang kann nicht versendet werden.")
            total_bytes += len(attachment.content)
            if total_bytes > _MAX_OUTBOUND_ATTACHMENT_TOTAL_BYTES:
                raise ValueError("Die Anhänge überschreiten zusammen das Zoho-Limit von 10 MB.")
            uploaded_ids.append(
                self.zoho_service.upload_file_to_zfs(
                    filename=filename,
                    content=attachment.content,
                    content_type=attachment.content_type or "application/octet-stream",
                )
            )
        return tuple(uploaded_ids)

    def has_loaded_email_content(self, email: CustomerZohoEmail) -> bool:
        """Return whether a full Zoho body was already stored for this header."""
        return "content" in self._payload(email.encrypted_payload_json)

    def load_email_content(self, *, customer_id: int, email_id: int) -> CustomerCommunicationActionResult:
        email = self.db.scalar(
            select(CustomerZohoEmail).where(
                CustomerZohoEmail.id == email_id,
                CustomerZohoEmail.customer_id == customer_id,
            )
        )
        if email is None:
            raise ValueError("Die E-Mail gehört nicht zu diesem Kunden.")
        self._load_email_content_for_email(email, mark_as_read=True)
        return CustomerCommunicationActionResult(True, "E-Mail-Inhalt wurde verschlüsselt aus Zoho geladen.")

    def _load_email_content_for_email(self, email: CustomerZohoEmail, *, mark_as_read: bool) -> None:
        if not email.zoho_message_id or not email.zoho_module or not email.zoho_record_id:
            raise ValueError("Für diese E-Mail ist kein Zoho-Inhalt verfügbar.")

        payload = self._payload(email.encrypted_payload_json)
        record = self.zoho_service.get_record_email(
            module=email.zoho_module,
            record_id=email.zoho_record_id,
            message_id=email.zoho_message_id,
            user_id=self._email_owner_id(payload),
        )
        if "content" not in record:
            raise ZohoCrmError("Zoho hat die E-Mail ohne Inhalt geliefert.")
        payload.update(record)
        email.encrypted_payload_json = self._encrypt_payload(payload)
        email.encrypted_header_json = self._encrypt_email_list_header(payload)
        email.zoho_synced_at = datetime.now(UTC)
        if mark_as_read:
            email.is_unread = False
        email.last_error = None
        self.db.flush()

    def mark_email_read(self, *, customer_id: int, email_id: int) -> None:
        email = self.db.scalar(
            select(CustomerZohoEmail).where(
                CustomerZohoEmail.id == email_id,
                CustomerZohoEmail.customer_id == customer_id,
            )
        )
        if email is None:
            raise ValueError("Die E-Mail gehört nicht zu diesem Kunden.")
        if email.direction == "inbound":
            if email.zoho_message_id:
                # A Zoho message can be displayed for several customers through shared contacts.
                # Reading it in one view must update every mirrored copy as one mailbox message.
                mirrored_emails = self.db.scalars(
                    select(CustomerZohoEmail).where(
                        CustomerZohoEmail.zoho_message_id == email.zoho_message_id,
                        CustomerZohoEmail.direction == "inbound",
                    )
                ).all()
                for mirrored_email in mirrored_emails:
                    mirrored_email.is_unread = False
            else:
                email.is_unread = False
            self.db.flush()

    def download_email_attachment(
        self,
        *,
        customer_id: int,
        email_id: int,
        attachment_id: str,
    ) -> CustomerCommunicationAttachmentDownload:
        email = self.db.scalar(
            select(CustomerZohoEmail).where(
                CustomerZohoEmail.id == email_id,
                CustomerZohoEmail.customer_id == customer_id,
            )
        )
        if email is None:
            raise ValueError("Die E-Mail gehört nicht zu diesem Kunden.")
        if not email.zoho_message_id or not email.zoho_module or not email.zoho_record_id:
            raise ValueError("Für diese E-Mail ist kein Zoho-Anhang verfügbar.")

        payload = self._payload(email.encrypted_payload_json)
        attachment = next((item for item in self._email_attachments(payload) if item.id == attachment_id), None)
        if attachment is None:
            raise ValueError("Der angeforderte Anhang gehört nicht zu dieser E-Mail.")
        owner_id = self._email_owner_id(payload)
        if owner_id is None:
            raise ValueError("Zoho hat für diesen Anhang keine abrufbare E-Mail-Owner-ID geliefert.")

        downloaded = self.zoho_service.download_record_email_attachment(
            module=email.zoho_module,
            record_id=email.zoho_record_id,
            message_id=email.zoho_message_id,
            user_id=owner_id,
            attachment_id=attachment.id,
            filename=attachment.filename,
        )
        return CustomerCommunicationAttachmentDownload(
            content=downloaded.content,
            content_type=downloaded.content_type,
            filename=attachment.filename,
        )

    def get_email_preview_image(
        self,
        *,
        customer_id: int,
        email_id: int,
        source_url_hash: str,
    ) -> CustomerCommunicationCachedImage:
        if not re.fullmatch(r"[0-9a-f]{64}", source_url_hash):
            raise ValueError("Das angeforderte Bild ist ungültig.")
        email = self.db.scalar(
            select(CustomerZohoEmail).where(
                CustomerZohoEmail.id == email_id,
                CustomerZohoEmail.customer_id == customer_id,
            )
        )
        if email is None:
            raise ValueError("Die E-Mail gehört nicht zu diesem Kunden.")

        payload = self._payload(email.encrypted_payload_json)
        source = self._email_image_sources(self._text(payload.get("content"))).get(source_url_hash)
        if source is None:
            raise ValueError("Das angeforderte Bild gehört nicht zu dieser E-Mail.")

        now = datetime.now(UTC)
        cached_image = self.db.scalar(
            select(CustomerZohoEmailImage).where(
                CustomerZohoEmailImage.email_id == email.id,
                CustomerZohoEmailImage.source_url_hash == source_url_hash,
            )
        )
        if cached_image is not None:
            if self._as_utc(cached_image.expires_at) > now:
                try:
                    return CustomerCommunicationCachedImage(
                        content=self.cipher.decrypt_bytes(cached_image.encrypted_image_bytes),
                        content_type=cached_image.content_type,
                    )
                except Exception:
                    # A damaged cache item must never block the original email preview.
                    self.db.delete(cached_image)
                    self.db.flush()
            else:
                self.db.delete(cached_image)
                self.db.flush()

        if source.kind == "zoho-inline":
            content, content_type = self._download_zoho_inline_image(email=email, payload=payload, image_id=source.value)
        else:
            content, content_type = self._download_external_image(source.value)
        self._expire_cached_email_images(now)
        self._make_email_image_cache_room(len(content))
        cached_image = CustomerZohoEmailImage(
            email=email,
            source_url_hash=source_url_hash,
            encrypted_image_bytes=self.cipher.encrypt_bytes(content),
            content_type=content_type,
            byte_size=len(content),
            expires_at=now + _EMAIL_IMAGE_CACHE_TTL,
        )
        self.db.add(cached_image)
        self.db.flush()
        return CustomerCommunicationCachedImage(content=content, content_type=content_type)

    def _upsert_zoho_note(
        self,
        *,
        customer: Customer,
        record: dict[str, object],
        synced_at: datetime,
        known_notes: dict[str, CustomerZohoNote],
    ) -> CustomerZohoEmail | None:
        note_id = self._text(record.get("id"))
        if not note_id:
            return False
        note = known_notes.get(note_id)
        created = note is None
        if note is None:
            note = CustomerZohoNote(
                customer=customer,
                zoho_note_id=note_id,
                source="zoho",
                sync_status="synced",
                encrypted_payload_json="",
            )
            self.db.add(note)
            known_notes[note_id] = note
        note.customer = customer
        note.source = "zoho"
        note.sync_status = "synced"
        note.encrypted_payload_json = self._encrypt_payload(record)
        note.created_by_username = self._name_from_value(record.get("Created_By"))
        note.zoho_created_at = self._datetime(record.get("Created_Time"))
        note.zoho_modified_at = self._datetime(record.get("Modified_Time"))
        note.zoho_synced_at = synced_at
        note.last_error = None
        return created

    def _upsert_zoho_email(
        self,
        *,
        customer: Customer,
        record: dict[str, object],
        module: str,
        record_id: str,
        synced_at: datetime,
        known_emails: dict[tuple[int, str], CustomerZohoEmail],
        mark_new_emails_unread: bool,
    ) -> bool:
        message_id = self._text(record.get("message_id")) or self._text(record.get("id"))
        if not message_id:
            return None
        key = (customer.id, message_id)
        email = known_emails.get(key)
        created = email is None
        if email is None:
            email = CustomerZohoEmail(
                customer=customer,
                zoho_message_id=message_id,
                source="zoho",
                direction=self._email_direction(record),
                is_unread=mark_new_emails_unread and self._email_direction(record) == "inbound",
                sync_status="synced",
                encrypted_payload_json="",
                encrypted_header_json="",
            )
            self.db.add(email)
            known_emails[key] = email
        email.customer = customer
        email.zoho_module = module
        email.zoho_record_id = record_id
        email.source = "zoho"
        email.direction = self._email_direction(record)
        email.sync_status = "synced"
        existing_payload = self._payload(email.encrypted_payload_json)
        merged_payload = dict(record)
        # Header refreshes omit the full body and detailed attachments. Keep them once loaded.
        for key in ("content", "attachments"):
            if key in existing_payload and key not in merged_payload:
                merged_payload[key] = existing_payload[key]
        email.encrypted_payload_json = self._encrypt_payload(merged_payload)
        email.encrypted_header_json = self._encrypt_email_list_header(merged_payload)
        email.zoho_sent_at = self._email_datetime(record)
        email.zoho_synced_at = synced_at
        email.last_error = None
        return email if created else None

    def _deduplicate_customer_emails(self, *, customer_id: int) -> int:
        """Merge the same Zoho email when it appears in both Account and Contact histories."""
        emails = self.db.scalars(
            select(CustomerZohoEmail)
            .where(CustomerZohoEmail.customer_id == customer_id)
            .order_by(CustomerZohoEmail.zoho_sent_at.asc(), CustomerZohoEmail.id.asc())
        ).all()
        retained: list[CustomerZohoEmail] = []
        removed_count = 0
        for email in emails:
            duplicate = next((item for item in retained if self._emails_represent_same_message(item, email)), None)
            if duplicate is None:
                retained.append(email)
                continue
            keeper, discarded = self._prefer_email_record(first=duplicate, second=email)
            if keeper is email:
                retained[retained.index(duplicate)] = email
            self.db.delete(discarded)
            removed_count += 1
        if removed_count:
            self.db.flush()
        return removed_count

    def _emails_represent_same_message(self, first: CustomerZohoEmail, second: CustomerZohoEmail) -> bool:
        first_location = (first.zoho_module, first.zoho_record_id)
        second_location = (second.zoho_module, second.zoho_record_id)
        if first_location == second_location or {first.zoho_module, second.zoho_module} != {ZOHO_ACCOUNT_MODULE, ZOHO_CONTACT_MODULE}:
            return False
        first_identity = self._email_semantic_identity(first)
        second_identity = self._email_semantic_identity(second)
        if first_identity is None or second_identity is None:
            return False
        first_subject, first_sender, first_recipients, first_time = first_identity
        second_subject, second_sender, second_recipients, second_time = second_identity
        return (
            first_subject == second_subject
            and first_sender == second_sender
            and first_recipients == second_recipients
            and abs((first_time - second_time).total_seconds()) <= 90
        )

    def _email_semantic_identity(
        self,
        email: CustomerZohoEmail,
    ) -> tuple[str, tuple[str, ...], tuple[str, ...], datetime] | None:
        payload = self._payload(email.encrypted_payload_json)
        subject = self._normalized_email_text(payload.get("subject"))
        sender = self._email_addresses(payload.get("from"))
        recipients = self._email_addresses(payload.get("to"))
        occurred_at = email.zoho_sent_at or self._email_datetime(payload)
        if not subject or not sender or not recipients or occurred_at is None:
            return None
        return subject, sender, recipients, self._as_utc(occurred_at)

    def _prefer_email_record(
        self,
        *,
        first: CustomerZohoEmail,
        second: CustomerZohoEmail,
    ) -> tuple[CustomerZohoEmail, CustomerZohoEmail]:
        def priority(email: CustomerZohoEmail) -> tuple[int, int, int, int]:
            payload = self._payload(email.encrypted_payload_json)
            return (
                int(bool(self._text(payload.get("content")))),
                len(self._email_attachments(payload)),
                int(email.zoho_module == ZOHO_CONTACT_MODULE),
                email.id,
            )

        return (first, second) if priority(first) >= priority(second) else (second, first)

    def _note_view(self, note: CustomerZohoNote) -> CustomerCommunicationNoteView:
        payload = self._payload(note.encrypted_payload_json)
        return CustomerCommunicationNoteView(
            id=note.id,
            title=self._text(payload.get("Note_Title")) or self._text(payload.get("title")) or "Ohne Titel",
            content=self._text(payload.get("Note_Content")) or self._text(payload.get("content")) or "",
            source=note.source,
            sync_status=note.sync_status,
            author=note.created_by_username,
            occurred_at=note.zoho_modified_at or note.zoho_created_at or note.created_at,
            last_error=note.last_error,
        )

    def _email_view(self, email: CustomerZohoEmail) -> CustomerCommunicationEmailView:
        payload = self._payload(email.encrypted_payload_json)
        content = self._text(payload.get("content"))
        return CustomerCommunicationEmailView(
            id=email.id,
            subject=self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._people_text(payload.get("from")),
            recipients=self._people_text(payload.get("to")),
            direction=email.direction,
            is_unread=email.is_unread,
            source=email.source,
            sync_status=email.sync_status,
            occurred_at=email.zoho_sent_at or email.created_at,
            preview_html=self._email_preview_document(
                content,
                image_url_prefix=f"/customers/{email.customer_id}/communications/emails/{email.id}/images",
            ),
            attachments=self._email_attachments(payload),
            can_load_content=content is None and bool(email.zoho_message_id and email.zoho_module and email.zoho_record_id),
            last_error=email.last_error,
        )

    @classmethod
    def _email_preview_document(cls, content: str | None, *, image_url_prefix: str | None = None) -> str | None:
        if content is None:
            return None
        if image_url_prefix:
            content = cls._rewrite_external_image_sources(content, image_url_prefix=image_url_prefix)
        security_head = (
            '<meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src data:; media-src data:; base-uri 'none'; form-action 'none'\">"
        )
        if not re.search(r"</?[a-z][^>]*>", content, flags=re.IGNORECASE):
            content = f"<pre>{escape(content)}</pre>"

        head_match = re.search(r"<head\\b[^>]*>", content, flags=re.IGNORECASE)
        if head_match:
            return f"{content[:head_match.end()]}{security_head}{content[head_match.end():]}"

        html_match = re.search(r"<html\\b[^>]*>", content, flags=re.IGNORECASE)
        if html_match:
            return f"{content[:html_match.end()]}<head>{security_head}</head>{content[html_match.end():]}"
        return f"<!doctype html><html><head>{security_head}</head><body>{content}</body></html>"

    @classmethod
    def _rewrite_external_image_sources(cls, content: str, *, image_url_prefix: str) -> str:
        def replace(match: re.Match[str]) -> str:
            source = cls._email_image_source(unescape(match.group("source")).strip())
            if source is None:
                return match.group(0)
            source_url_hash = cls._image_source_hash(source)
            return f'{match.group("prefix")}{match.group("quote")}{image_url_prefix}/{source_url_hash}{match.group("quote")}'

        return _IMAGE_SRC_PATTERN.sub(replace, content)

    @classmethod
    def _email_image_sources(cls, content: str | None) -> dict[str, CustomerCommunicationImageSource]:
        if not content:
            return {}
        sources: dict[str, CustomerCommunicationImageSource] = {}
        for match in _IMAGE_SRC_PATTERN.finditer(content):
            source = cls._email_image_source(unescape(match.group("source")).strip())
            if source is not None:
                sources[cls._image_source_hash(source)] = source
        return sources

    @classmethod
    def _email_image_source(cls, source_url: str) -> CustomerCommunicationImageSource | None:
        if cls._is_external_image_source(source_url):
            return CustomerCommunicationImageSource(kind="external", value=source_url)
        inline_match = _ZOHO_INLINE_IMAGE_SOURCE_PATTERN.fullmatch(source_url)
        if inline_match:
            return CustomerCommunicationImageSource(kind="zoho-inline", value=inline_match.group("image_id"))
        return None

    @staticmethod
    def _image_source_hash(source: CustomerCommunicationImageSource) -> str:
        value = source.value if source.kind == "external" else f"{source.kind}:{source.value}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_external_image_source(source_url: str) -> bool:
        try:
            parsed = urlsplit(source_url)
            port = parsed.port
        except ValueError:
            return False
        if not parsed.hostname:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return False
        except ValueError:
            pass
        return (
            parsed.username is None
            and parsed.password is None
            and port in (None, 443 if parsed.scheme == "https" else 80)
        )

    @classmethod
    def _download_external_image(cls, source_url: str) -> tuple[bytes, str]:
        current_url = source_url
        opener = build_opener(_NoRedirect())
        for _ in range(4):
            cls._validate_external_image_url(current_url)
            request = Request(
                current_url,
                headers={
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.8,*/*;q=0.1",
                    "User-Agent": "kosmos-hub-email-image-cache/1.0",
                },
            )
            try:
                with opener.open(request, timeout=_EXTERNAL_IMAGE_TIMEOUT_SECONDS) as response:
                    content_type = response.headers.get_content_type().casefold()
                    if content_type not in _ALLOWED_IMAGE_CONTENT_TYPES:
                        raise CustomerCommunicationImageError("Das externe Bild hat kein erlaubtes Bildformat.")
                    content = response.read(_MAX_EXTERNAL_IMAGE_BYTES + 1)
                    if not content or len(content) > _MAX_EXTERNAL_IMAGE_BYTES:
                        raise CustomerCommunicationImageError("Das externe Bild überschreitet das Größenlimit.")
                    return content, content_type
            except HTTPError as exc:
                if 300 <= exc.code < 400:
                    location = exc.headers.get("Location")
                    if location:
                        current_url = urljoin(current_url, location)
                        continue
                raise CustomerCommunicationImageError("Das externe Bild konnte nicht abgerufen werden.") from exc
            except (OSError, URLError, ValueError) as exc:
                raise CustomerCommunicationImageError("Das externe Bild konnte nicht abgerufen werden.") from exc
        raise CustomerCommunicationImageError("Das externe Bild leitet zu oft weiter.")

    def _download_zoho_inline_image(
        self,
        *,
        email: CustomerZohoEmail,
        payload: dict[str, object],
        image_id: str,
    ) -> tuple[bytes, str]:
        if not email.zoho_message_id or not email.zoho_module or not email.zoho_record_id:
            raise CustomerCommunicationImageError("Für dieses Zoho-Inline-Bild fehlen die Abrufdaten.")
        owner_id = self._email_owner_id(payload)
        if owner_id is None:
            raise CustomerCommunicationImageError("Zoho hat für dieses Inline-Bild keine Owner-ID geliefert.")
        downloaded = self.zoho_service.download_record_email_inline_image(
            module=email.zoho_module,
            record_id=email.zoho_record_id,
            message_id=email.zoho_message_id,
            user_id=owner_id,
            image_id=image_id,
        )
        content_type = downloaded.content_type.casefold()
        if content_type not in _ALLOWED_IMAGE_CONTENT_TYPES:
            raise CustomerCommunicationImageError("Das Zoho-Inline-Bild hat kein erlaubtes Bildformat.")
        if not downloaded.content or len(downloaded.content) > _MAX_EXTERNAL_IMAGE_BYTES:
            raise CustomerCommunicationImageError("Das Zoho-Inline-Bild überschreitet das Größenlimit.")
        return downloaded.content, content_type

    @staticmethod
    def _validate_external_image_url(source_url: str) -> None:
        try:
            parsed = urlsplit(source_url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise CustomerCommunicationImageError("Die externe Bildadresse ist ungültig.") from exc
        if not hostname or not CustomerCommunicationService._is_external_image_source(source_url):
            raise CustomerCommunicationImageError("Die externe Bildadresse ist nicht erlaubt.")
        port = 443 if parsed.scheme == "https" else 80
        try:
            addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise CustomerCommunicationImageError("Der Bildserver ist nicht erreichbar.") from exc
        if not addresses:
            raise CustomerCommunicationImageError("Der Bildserver ist nicht erreichbar.")
        for address in addresses:
            try:
                if not ipaddress.ip_address(address[4][0]).is_global:
                    raise CustomerCommunicationImageError("Die externe Bildadresse ist nicht erlaubt.")
            except ValueError as exc:
                raise CustomerCommunicationImageError("Die externe Bildadresse ist ungültig.") from exc

    def _expire_cached_email_images(self, now: datetime) -> None:
        self.db.execute(
            delete(CustomerZohoEmailImage).where(CustomerZohoEmailImage.expires_at <= now)
        )
        self.db.flush()

    def _make_email_image_cache_room(self, incoming_size: int) -> None:
        if incoming_size > _MAX_EMAIL_IMAGE_CACHE_BYTES:
            raise CustomerCommunicationImageError("Das externe Bild überschreitet das Cache-Limit.")
        cached_size = self.db.scalar(select(func.coalesce(func.sum(CustomerZohoEmailImage.byte_size), 0))) or 0
        remaining_to_remove = cached_size + incoming_size - _MAX_EMAIL_IMAGE_CACHE_BYTES
        if remaining_to_remove <= 0:
            return
        for cached_image in self.db.scalars(
            select(CustomerZohoEmailImage).order_by(CustomerZohoEmailImage.created_at.asc(), CustomerZohoEmailImage.id.asc())
        ).all():
            self.db.delete(cached_image)
            remaining_to_remove -= cached_image.byte_size
            if remaining_to_remove <= 0:
                break
        self.db.flush()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @classmethod
    def _email_attachments(cls, payload: dict[str, object]) -> tuple[CustomerCommunicationAttachment, ...]:
        raw_attachments = payload.get("attachments")
        if not isinstance(raw_attachments, list):
            return ()
        attachments: list[CustomerCommunicationAttachment] = []
        seen_ids: set[str] = set()
        for item in raw_attachments:
            if not isinstance(item, dict):
                continue
            attachment_id = cls._text(item.get("id")) or cls._text(item.get("attachment_id"))
            filename = cls._text(item.get("name")) or cls._text(item.get("file_name"))
            if not attachment_id or not filename or attachment_id in seen_ids:
                continue
            seen_ids.add(attachment_id)
            attachments.append(CustomerCommunicationAttachment(id=attachment_id, filename=filename))
        return tuple(attachments)

    @classmethod
    def _email_owner_id(cls, payload: dict[str, object]) -> str | None:
        for key in ("owner", "Owner", "user"):
            value = payload.get(key)
            if isinstance(value, dict):
                owner_id = cls._text(value.get("id"))
                if owner_id:
                    return owner_id
        return None

    def _recipients_for_customer(self, customer: Customer) -> list[CustomerCommunicationRecipient]:
        recipients: list[CustomerCommunicationRecipient] = []
        seen: set[str] = set()

        def add_recipient(*, key: str, name: str, email: object) -> None:
            parsed_name, parsed_email = parseaddr(str(email or ""))
            normalized_email = parsed_email.strip().casefold()
            if not normalized_email or "@" not in normalized_email or normalized_email in seen:
                return
            seen.add(normalized_email)
            recipients.append(
                CustomerCommunicationRecipient(
                    key=key,
                    name=(name or parsed_name or customer.name).strip()[:255],
                    email=normalized_email,
                )
            )

        profile = self._payload(customer.encrypted_profile_json)
        fields = profile.get("fields") if isinstance(profile.get("fields"), dict) else {}
        add_recipient(key=f"account:{self._text(fields.get('Kontakt-E-Mail')) or ''}", name=customer.name, email=fields.get("Kontakt-E-Mail"))
        for contact in self.db.scalars(
            select(CustomerContact).where(CustomerContact.customer_id == customer.id)
        ).all():
            contact_profile = self._payload(contact.encrypted_profile_json)
            contact_fields = contact_profile.get("fields") if isinstance(contact_profile.get("fields"), dict) else {}
            name = self._text(contact_fields.get("Name")) or customer.name
            for label in ("E-Mail", "Zweite E-Mail-Adresse", "Dritte E-Mail-Adresse"):
                address = self._text(contact_fields.get(label))
                add_recipient(key=f"contact:{contact.id}:{address or ''}", name=name, email=address)
        return recipients

    def _require_customer(self, customer_id: int) -> Customer:
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            raise ValueError("Kunde nicht gefunden.")
        return customer

    def _require_customer_email(self, *, customer_id: int, email_id: int) -> CustomerZohoEmail:
        email = self.db.scalar(
            select(CustomerZohoEmail).where(
                CustomerZohoEmail.id == email_id,
                CustomerZohoEmail.customer_id == customer_id,
            )
        )
        if email is None:
            raise ValueError("Die E-Mail gehört nicht zu diesem Kunden.")
        return email

    def _require_zoho_customer(self, customer_id: int) -> Customer:
        customer = self._require_customer(customer_id)
        if not customer.zoho_id:
            raise ValueError("Dieser Kunde besitzt keine verknüpfte Zoho-Account-ID.")
        return customer

    def _stored_email_template(self, template_id: str) -> ZohoEmailTemplate:
        normalized_template_id = self._required_template_id(template_id)
        template = self.db.scalar(
            select(ZohoEmailTemplate).where(
                ZohoEmailTemplate.zoho_template_id == normalized_template_id,
                ZohoEmailTemplate.is_active.is_(True),
            )
        )
        if template is None:
            raise ValueError("Diese Zoho-E-Mail-Vorlage ist nicht im Hub synchronisiert.")
        return template

    @staticmethod
    def _required_template_module(value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", normalized):
            raise ValueError("Zoho returned an invalid email template module.")
        return normalized

    @classmethod
    def _template_module_from_record(cls, record: dict[str, object], *, fallback: str | None = None) -> str:
        module = record.get("module")
        module_api_name = cls._text(module.get("api_name")) if isinstance(module, dict) else None
        return cls._required_template_module(module_api_name or fallback or "")

    def _email_template_context(
        self,
        *,
        customer: Customer,
        recipient: CustomerCommunicationRecipient | None,
    ) -> dict[str, str]:
        context: dict[str, str] = {}

        def add(value: object, *keys: str) -> None:
            text = self._text(value)
            if not text:
                return
            for key in keys:
                normalized_key = self._normalized_template_key(key)
                if normalized_key:
                    context[normalized_key] = text

        add(
            customer.name,
            "Account Name",
            "Account_Name",
            "Accounts.Account Name",
            "Accounts.Account_Name",
            "Customer.Name",
        )
        add(customer.zoho_id, "id", "Accounts.id", "Account.id")
        profile = self._payload(customer.encrypted_profile_json)
        fields = profile.get("fields") if isinstance(profile.get("fields"), dict) else {}
        aliases_by_label = {
            "Eintrag-ID": ("id", "Accounts.id", "Account.id"),
            "Update-Datum": ("Dialfire_WV_Datum", "Accounts.Dialfire_WV_Datum"),
            "Update-Notiz": ("Dialfire_WV_Notiz", "Accounts.Dialfire_WV_Notiz"),
        }
        for label, value in fields.items():
            if not isinstance(label, str):
                continue
            add(value, label, f"Accounts.{label}", f"Account.{label}", *aliases_by_label.get(label, ()))
        if recipient is not None:
            add(recipient.name, "Full Name", "Full_Name", "Contacts.Full Name", "Contacts.Full_Name", "Contact.Name")
            add(recipient.email, "Email", "Contacts.Email", "Contact.Email")
        return context

    @staticmethod
    def _normalized_template_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    @classmethod
    def _resolve_template_placeholders(
        cls,
        value: str,
        *,
        context: dict[str, str],
        html: bool,
    ) -> tuple[str, tuple[str, ...]]:
        unresolved: list[str] = []

        def replace(match: re.Match[str]) -> str:
            key = (match.group("dollar") or match.group("brace") or "").strip()
            replacement = context.get(cls._normalized_template_key(key))
            if replacement is None:
                unresolved.append(key)
                return match.group(0)
            return escape(replacement) if html else replacement

        resolved = re.sub(
            r"\$\{\s*(?P<dollar>[^}]+?)\s*\}|\{\{\s*(?P<brace>.+?)\s*\}\}",
            replace,
            value,
        )
        return resolved, tuple(unresolved)

    def _encrypt_payload(self, payload: dict[str, object]) -> str:
        return self.cipher.encrypt(json.dumps(payload, ensure_ascii=False, default=str))

    def _encrypt_email_list_header(self, payload: dict[str, object]) -> str:
        return self._encrypt_payload(self.email_list_header_payload(payload))

    @classmethod
    def email_list_header_payload(cls, payload: dict[str, object]) -> dict[str, object]:
        """Store only the list fields separately so mailbox rows never need the full body."""
        return {
            "subject": cls._text(payload.get("subject")),
            "sender": cls._people_text(payload.get("from")),
            "recipients": cls._people_text(payload.get("to")),
        }

    def _payload(self, encrypted_payload: str | None) -> dict[str, object]:
        if not encrypted_payload:
            return {}
        try:
            payload = json.loads(self.cipher.decrypt(encrypted_payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _required_text(value: str, label: str, *, maximum: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{label} darf nicht leer sein.")
        if len(normalized) > maximum:
            raise ValueError(f"{label} darf höchstens {maximum:,} Zeichen enthalten.")
        return normalized

    @staticmethod
    def _note_title_from_content(content: str) -> str:
        """Zoho requires a title; use the first written line when the form omits it."""
        return next(line.strip() for line in content.splitlines() if line.strip())[:255]

    @staticmethod
    def _required_template_id(value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", normalized):
            raise ValueError("Wähle eine gültige Zoho-E-Mail-Vorlage aus.")
        return normalized

    @classmethod
    def _sanitized_email_content(cls, value: str) -> str:
        sanitizer = _EmailComposerHtmlSanitizer()
        sanitizer.feed(value)
        sanitizer.close()
        normalized = sanitizer.content().strip()
        plain_text = unescape(re.sub(r"<[^>]+>", "", normalized)).strip()
        if not plain_text:
            raise ValueError("Nachricht darf nicht leer sein.")
        if len(normalized) > 50_000:
            raise ValueError("Nachricht darf höchstens 50,000 Zeichen enthalten.")
        return normalized

    @staticmethod
    def _text(value: object) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _normalized_email_text(cls, value: object) -> str | None:
        text = cls._text(value)
        return re.sub(r"\s+", " ", text).casefold() if text else None

    @classmethod
    def _email_addresses(cls, value: object) -> tuple[str, ...]:
        values = value if isinstance(value, list) else [value]
        addresses: set[str] = set()
        for item in values:
            if isinstance(item, dict):
                candidate = cls._text(item.get("email"))
            else:
                _name, candidate = parseaddr(str(item or ""))
            if candidate and "@" in candidate:
                addresses.add(candidate.strip().casefold())
        return tuple(sorted(addresses))

    @classmethod
    def _cc_recipients(
        cls,
        value: str,
        *,
        excluded_emails: set[str],
    ) -> tuple[tuple[str, str], ...]:
        """Parse editable CC input while preventing duplicate primary recipients."""
        if not value.strip():
            return ()
        excluded = {email.strip().casefold() for email in excluded_emails}
        recipients: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name, email in getaddresses([value.replace(";", ",")]):
            normalized_email = email.strip().casefold()
            if not normalized_email:
                continue
            if not re.fullmatch(r"[^@\s]+@[^@\s]+", normalized_email):
                raise ValueError("CC enthält keine gültige E-Mail-Adresse.")
            if normalized_email in excluded or normalized_email in seen:
                continue
            seen.add(normalized_email)
            recipients.append(((name.strip() or normalized_email)[:255], normalized_email))
        if not recipients:
            raise ValueError("CC enthält keine zusätzliche gültige E-Mail-Adresse.")
        if len(recipients) > 20:
            raise ValueError("CC darf höchstens 20 E-Mail-Adressen enthalten.")
        return tuple(recipients)

    @classmethod
    def _people_text(cls, value: object) -> str | None:
        if isinstance(value, list):
            people = [cls._people_text(item) for item in value]
            return ", ".join(person for person in people if person) or None
        if isinstance(value, dict):
            name = cls._text(value.get("user_name")) or cls._text(value.get("name"))
            email = cls._text(value.get("email"))
            if name and email:
                return f"{name} <{email}>"
            return name or email
        return cls._text(value)

    @classmethod
    def _name_from_value(cls, value: object) -> str | None:
        if isinstance(value, dict):
            return cls._text(value.get("name")) or cls._text(value.get("full_name"))
        return cls._text(value)

    @classmethod
    def _email_datetime(cls, payload: dict[str, object]) -> datetime | None:
        for key in ("received_time", "sent_time", "time", "created_time"):
            parsed = cls._datetime(payload.get(key))
            if parsed is not None:
                return parsed
        return None

    @classmethod
    def _email_direction(cls, payload: dict[str, object]) -> str:
        direction = cls._text(payload.get("direction"))
        if direction in {"inbound", "outbound"}:
            return direction
        if payload.get("sent") is True:
            return "outbound"
        if payload.get("sent") is False:
            return "inbound"
        return "unknown"

    @staticmethod
    def _datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
