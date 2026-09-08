"""SMTP delivery through configured Mittwald mailboxes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from html import unescape
import re
import smtplib
import ssl

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_mailbox_account import HubMailboxAccount


_CONNECTION_TIMEOUT_SECONDS = 30
_MAX_ATTACHMENT_COUNT = 20
_MAX_ATTACHMENT_TOTAL_BYTES = 50 * 1024 * 1024
_MAX_INLINE_IMAGE_COUNT = 10
DEFAULT_HUB_MAILBOX_SENDER_EMAIL = "info@kosmos-medien.de"
_BODY_CONTENT_PATTERN = re.compile(r"<body\b[^>]*>(?P<content>.*?)</body\s*>", flags=re.IGNORECASE | re.DOTALL)
_INVISIBLE_CONTENT_PATTERN = re.compile(
    r"<(?:head|style|script|title)\b[^>]*>.*?</(?:head|style|script|title)\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", flags=re.DOTALL)


class HubMailboxTransportError(ValueError):
    """A safe error raised when a Mittwald SMTP delivery cannot be completed."""


@dataclass(frozen=True)
class HubMailboxTransportSender:
    name: str
    email: str


@dataclass(frozen=True)
class HubMailboxTransportAttachment:
    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class HubMailboxTransportInlineImage:
    content_id: str
    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class HubMailboxTransportDelivery:
    message_id: str
    sent_at: datetime


class HubMailboxTransportService:
    """Deliver Hub-composed mail through Mittwald without involving Zoho."""

    def __init__(self, *, db: Session, cipher: SecretCipher) -> None:
        self.db = db
        self.cipher = cipher

    def list_senders(self) -> tuple[HubMailboxTransportSender, ...]:
        accounts = self.db.scalars(
            select(HubMailboxAccount)
            .where(HubMailboxAccount.enabled.is_(True), HubMailboxAccount.verified_at.is_not(None))
            .order_by(HubMailboxAccount.email_address.asc())
        ).all()
        senders = (
            HubMailboxTransportSender(name=account.display_name or account.email_address, email=account.email_address)
            for account in accounts
        )
        return tuple(sorted(
            senders,
            key=lambda sender: (
                sender.email.casefold() != DEFAULT_HUB_MAILBOX_SENDER_EMAIL,
                sender.email.casefold(),
            ),
        ))

    def is_configured(self) -> bool:
        return bool(self.list_senders())

    def send(
        self,
        *,
        sender_email: str,
        recipient_name: str,
        recipient_email: str,
        subject: str,
        html_content: str,
        cc_recipients: tuple[tuple[str, str], ...],
        reply_to_message_id: str | None,
        attachments: tuple[HubMailboxTransportAttachment, ...],
        inline_images: tuple[HubMailboxTransportInlineImage, ...] = (),
        message_id: str | None = None,
    ) -> HubMailboxTransportDelivery:
        account = self._sender_account(sender_email)
        attachment_bytes = self._validate_attachments(attachments)
        inline_image_bytes = self._validate_inline_images(inline_images)
        if attachment_bytes + inline_image_bytes > _MAX_ATTACHMENT_TOTAL_BYTES:
            raise HubMailboxTransportError("Anhänge und eingefügte Bilder sind zusammen größer als 50 MB.")
        now = datetime.now(UTC)
        domain = account.email_address.partition("@")[2] or "localhost"
        delivery_message_id = message_id or make_msgid(domain=domain)
        message = EmailMessage()
        message["From"] = formataddr((account.display_name or account.email_address, account.email_address))
        message["To"] = formataddr((recipient_name or recipient_email, recipient_email))
        if cc_recipients:
            message["Cc"] = ", ".join(formataddr((name or email, email)) for name, email in cc_recipients)
        message["Subject"] = subject
        message["Date"] = format_datetime(now)
        message["Message-ID"] = delivery_message_id
        if reply_to_message_id:
            message["In-Reply-To"] = reply_to_message_id
            message["References"] = reply_to_message_id
        message.set_content(self._plain_text(html_content))
        message.add_alternative(html_content, subtype="html")
        html_part = message.get_payload()[-1]
        for image in inline_images:
            maintype, _, subtype = image.content_type.partition("/")
            html_part.add_related(
                image.content,
                maintype=maintype or "image",
                subtype=subtype or "png",
                cid=f"<{image.content_id}>",
                filename=image.filename,
                disposition="inline",
            )
        for attachment in attachments:
            maintype, _, subtype = (attachment.content_type or "application/octet-stream").partition("/")
            message.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )

        smtp = None
        try:
            smtp = smtplib.SMTP_SSL(
                account.smtp_host,
                account.smtp_port,
                context=ssl.create_default_context(),
                timeout=_CONNECTION_TIMEOUT_SECONDS,
            )
            smtp.login(account.username, self.cipher.decrypt(account.encrypted_password))
            smtp.send_message(
                message,
                from_addr=account.email_address,
                to_addrs=[recipient_email, *(email for _name, email in cc_recipients)],
            )
        except smtplib.SMTPAuthenticationError as exc:
            raise HubMailboxTransportError("Die SMTP-Anmeldung bei Mittwald wurde abgelehnt.") from exc
        except (OSError, ValueError, smtplib.SMTPException) as exc:
            raise HubMailboxTransportError("Die E-Mail konnte nicht über Mittwald versendet werden. Bitte später erneut versuchen.") from exc
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except (OSError, smtplib.SMTPException):
                    pass
        return HubMailboxTransportDelivery(message_id=delivery_message_id, sent_at=now)

    def _sender_account(self, sender_email: str) -> HubMailboxAccount:
        normalized = sender_email.strip().casefold()
        account = self.db.scalar(
            select(HubMailboxAccount).where(
                HubMailboxAccount.email_address == normalized,
                HubMailboxAccount.enabled.is_(True),
                HubMailboxAccount.verified_at.is_not(None),
            )
        )
        if account is None:
            raise HubMailboxTransportError("Wähle ein eingerichtetes Mittwald-Postfach als Absender aus.")
        return account

    @staticmethod
    def _validate_attachments(attachments: tuple[HubMailboxTransportAttachment, ...]) -> int:
        if len(attachments) > _MAX_ATTACHMENT_COUNT:
            raise HubMailboxTransportError("Es können höchstens 20 Anhänge pro E-Mail versendet werden.")
        total_bytes = 0
        for attachment in attachments:
            if not attachment.filename.strip() or len(attachment.filename) > 255:
                raise HubMailboxTransportError("Ein Anhang besitzt keinen gültigen Dateinamen.")
            if not attachment.content:
                raise HubMailboxTransportError("Ein leerer Anhang kann nicht versendet werden.")
            total_bytes += len(attachment.content)
            if total_bytes > _MAX_ATTACHMENT_TOTAL_BYTES:
                raise HubMailboxTransportError("Die Anhänge sind zusammen größer als 50 MB.")
        return total_bytes

    @staticmethod
    def _validate_inline_images(images: tuple[HubMailboxTransportInlineImage, ...]) -> int:
        if len(images) > _MAX_INLINE_IMAGE_COUNT:
            raise HubMailboxTransportError("Es können höchstens 10 Bilder pro E-Mail eingefügt werden.")
        total_bytes = 0
        for image in images:
            if not re.fullmatch(r"[A-Za-z0-9@._-]{1,255}", image.content_id):
                raise HubMailboxTransportError("Ein eingefügtes Bild besitzt keine gültige Kennung.")
            if not image.filename.strip() or len(image.filename) > 255:
                raise HubMailboxTransportError("Ein eingefügtes Bild besitzt keinen gültigen Dateinamen.")
            if not image.content:
                raise HubMailboxTransportError("Ein eingefügtes Bild ist leer.")
            if not image.content_type.casefold().startswith("image/"):
                raise HubMailboxTransportError("Ein eingefügtes Bild besitzt keinen gültigen Dateityp.")
            total_bytes += len(image.content)
            if total_bytes > _MAX_ATTACHMENT_TOTAL_BYTES:
                raise HubMailboxTransportError("Die eingefügten Bilder sind zusammen größer als 50 MB.")
        return total_bytes

    @staticmethod
    def _plain_text(html_content: str) -> str:
        """Create a text alternative from visible body content, never compiler CSS."""
        body_match = _BODY_CONTENT_PATTERN.search(html_content)
        text = body_match.group("content") if body_match else html_content
        text = _INVISIBLE_CONTENT_PATTERN.sub("", text)
        text = _HTML_COMMENT_PATTERN.sub("", text)
        text = re.sub(r"<img\b[^>]*>", " [Bild] ", text, flags=re.IGNORECASE)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<li\b[^>]*>", "- ", text, flags=re.IGNORECASE)
        text = re.sub(r"</(?:p|div|li|tr|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(?:td|th)\s*>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        lines = (" ".join(unescape(line).split()) for line in text.splitlines())
        return "\n".join(line for line in lines if line).strip() or " "
