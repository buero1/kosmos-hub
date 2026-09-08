"""Read selected Mittwald IMAP folders into the Hub without changing the mailbox."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from hashlib import sha256
import imaplib
import re
import ssl
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_communication import CustomerEmailAttachment, CustomerZohoEmail
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_email import HubMailboxAttachment, HubMailboxEmail
from app.models.hub_mailbox_imap_import import HubMailboxImapImport, HubMailboxImapImportItem
from app.services.customer_communications import CustomerCommunicationService
from app.services.email_attachment_storage import EmailAttachmentStorageError


_ACTIVE_STATUSES = ("pending", "running")
_FOLDERS = ("INBOX", "INBOX.Sent")
_CONNECTION_TIMEOUT_SECONDS = 30
_MAX_MESSAGE_BYTES = 100 * 1024 * 1024


class HubMailboxImapImportError(ValueError):
    """A safe error raised while selecting or importing Mittwald mail."""


@dataclass(frozen=True)
class HubMailboxImapImportStatus:
    id: int
    since_date: date
    status: str
    total_messages: int
    processed_messages: int
    imported_messages: int
    skipped_messages: int
    failed_messages: int
    stored_attachments: int
    stored_bytes: int
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class _ParsedAttachment:
    id: str
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class _ParsedMessage:
    identity: str
    payload: dict[str, object]
    occurred_at: datetime
    is_unread: bool
    attachments: tuple[_ParsedAttachment, ...]


class HubMailboxImapImportService:
    """Persist selected IMAP UIDs first, then fetch one message at a time safely."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        public_base_url: str,
    ) -> None:
        self.db = db
        self.cipher = cipher
        self.communications = CustomerCommunicationService(
            db=db,
            cipher=cipher,
            public_base_url=public_base_url,
        )

    def status(self) -> HubMailboxImapImportStatus | None:
        latest = self.db.scalar(select(HubMailboxImapImport).order_by(HubMailboxImapImport.id.desc()).limit(1))
        return self._status(latest) if latest is not None else None

    def start(self, *, requested_by: str, since_date: date) -> tuple[HubMailboxImapImportStatus, bool]:
        active = self._active_import()
        if active is not None:
            return self._status(active), False

        accounts = self.db.scalars(
            select(HubMailboxAccount)
            .where(HubMailboxAccount.enabled.is_(True), HubMailboxAccount.verified_at.is_not(None))
            .order_by(HubMailboxAccount.email_address.asc())
        ).all()
        if not accounts:
            raise HubMailboxImapImportError("Bitte zuerst mindestens ein Mittwald-Postfach erfolgreich testen.")
        self.communications.ensure_email_attachment_storage()

        selected: list[tuple[int, str, str]] = []
        for account in accounts:
            for folder in _FOLDERS:
                for uid in self._list_uids(account=account, folder=folder, since_date=since_date):
                    selected.append((account.id, folder, uid))
        if not selected:
            raise HubMailboxImapImportError(
                f"Seit dem {since_date.strftime('%d.%m.%Y')} wurden in Posteingang und Gesendet keine E-Mails gefunden."
            )

        mailbox_import = HubMailboxImapImport(
            requested_by=requested_by[:128],
            since_date=since_date,
            status="pending",
            total_messages=len(selected),
        )
        self.db.add(mailbox_import)
        self.db.flush()
        self.db.add_all(
            HubMailboxImapImportItem(
                mailbox_import_id=mailbox_import.id,
                mailbox_account_id=account_id,
                folder=folder,
                imap_uid=uid,
            )
            for account_id, folder, uid in selected
        )
        self.db.flush()
        return self._status(mailbox_import), True

    def process_next_message(self) -> str | None:
        """Fetch exactly one durable work item, so restart recovery never loses progress."""
        mailbox_import = self._active_import()
        if mailbox_import is None:
            return None
        if mailbox_import.cancel_requested:
            mailbox_import.status = "cancelled"
            mailbox_import.completed_at = datetime.now(UTC)
            mailbox_import.last_error = "Der Mittwald-Import wurde durch den Benutzer abgebrochen."
            self.db.commit()
            return "cancelled"
        if mailbox_import.status == "pending":
            mailbox_import.status = "running"
            mailbox_import.started_at = datetime.now(UTC)

        item = self.db.scalar(
            select(HubMailboxImapImportItem)
            .where(
                HubMailboxImapImportItem.mailbox_import_id == mailbox_import.id,
                HubMailboxImapImportItem.status == "pending",
            )
            .order_by(HubMailboxImapImportItem.id.asc())
            .limit(1)
        )
        if item is None:
            mailbox_import.status = "completed"
            mailbox_import.completed_at = datetime.now(UTC)
            mailbox_import.last_error = None
            self.db.commit()
            return "completed"

        import_id = mailbox_import.id
        item_id = item.id
        account_id = item.mailbox_account_id
        folder = item.folder
        imap_uid = item.imap_uid
        # Do not keep a database transaction open during the remote IMAP request.
        self.db.commit()
        try:
            account = self.db.get(HubMailboxAccount, account_id)
            if account is None or not account.enabled:
                raise HubMailboxImapImportError("Das ausgewählte Mittwald-Postfach ist nicht mehr aktiv.")
            raw_message, flags = self._fetch_message(account=account, folder=folder, uid=imap_uid)
            parsed = self._parse_message(
                raw_message=raw_message,
                account=account,
                folder=folder,
                uid=imap_uid,
                flags=flags,
            )
            outcome, attachment_count, attachment_bytes = self._store_message(parsed=parsed)
        except (EmailAttachmentStorageError, HubMailboxImapImportError, ValueError) as exc:
            self.db.rollback()
            return self._record_failure(import_id=import_id, item_id=item_id, error=str(exc))
        except Exception:
            self.db.rollback()
            return self._record_failure(
                import_id=import_id,
                item_id=item_id,
                error="Die E-Mail konnte nicht sicher aus dem Mittwald-Postfach übernommen werden.",
            )

        mailbox_import = self.db.get(HubMailboxImapImport, import_id)
        item = self.db.get(HubMailboxImapImportItem, item_id)
        if mailbox_import is None or item is None:
            self.db.rollback()
            return None
        item.status = outcome
        item.last_error = None
        mailbox_import.processed_messages += 1
        if outcome == "imported":
            mailbox_import.imported_messages += 1
            mailbox_import.stored_attachments += attachment_count
            mailbox_import.stored_bytes += attachment_bytes
        else:
            mailbox_import.skipped_messages += 1
        mailbox_import.consecutive_failures = 0
        mailbox_import.last_error = None
        self.db.commit()
        return "succeeded" if outcome == "imported" else "skipped"

    def _store_message(self, *, parsed: _ParsedMessage) -> tuple[str, int, int]:
        """Keep one Mittwald message once, preferring existing Zoho data for the same Message-ID."""
        existing_linked = self.db.scalar(
            select(CustomerZohoEmail.id)
            .where(CustomerZohoEmail.zoho_message_id == parsed.identity)
            .limit(1)
        )
        fingerprint = sha256(f"mittwald-imap:{parsed.identity}".encode("utf-8")).hexdigest()
        existing_unassigned = self.db.scalar(
            select(HubMailboxEmail.id).where(HubMailboxEmail.fingerprint == fingerprint).limit(1)
        )
        if existing_linked is not None or existing_unassigned is not None:
            return "skipped", 0, 0

        matched_customers = self._matched_customers(parsed.payload)
        stored_keys: list[str] = []
        try:
            if matched_customers:
                for customer in matched_customers:
                    email = CustomerZohoEmail(
                        customer_id=customer.id,
                        zoho_message_id=parsed.identity,
                        source="mittwald-imap",
                        direction=str(parsed.payload["direction"]),
                        is_unread=parsed.is_unread,
                        encrypted_payload_json=self.communications._encrypt_payload(parsed.payload),
                        encrypted_header_json=self.communications._encrypt_email_list_header(parsed.payload),
                        zoho_sent_at=parsed.occurred_at,
                        zoho_synced_at=datetime.now(UTC),
                    )
                    self.db.add(email)
                    self.db.flush()
                    stored_keys.extend(self._store_linked_attachments(email=email, attachments=parsed.attachments))
            else:
                email = HubMailboxEmail(
                    source="mittwald-imap",
                    direction=str(parsed.payload["direction"]),
                    is_unread=parsed.is_unread,
                    fingerprint=fingerprint,
                    encrypted_payload_json=self.communications._encrypt_payload(parsed.payload),
                    received_at=parsed.occurred_at,
                )
                self.db.add(email)
                self.db.flush()
                stored_keys.extend(self._store_unassigned_attachments(email=email, attachments=parsed.attachments))
            self.db.flush()
        except Exception:
            for storage_key in stored_keys:
                self.communications.attachment_storage.remove(storage_key)
            raise
        return "imported", len(parsed.attachments), sum(len(attachment.content) for attachment in parsed.attachments)

    def _store_linked_attachments(
        self,
        *,
        email: CustomerZohoEmail,
        attachments: tuple[_ParsedAttachment, ...],
    ) -> list[str]:
        stored_keys: list[str] = []
        for attachment in attachments:
            storage_key = self.communications.attachment_storage.store(attachment.content)
            stored_keys.append(storage_key)
            self.db.add(
                CustomerEmailAttachment(
                    email_id=email.id,
                    source="mittwald-imap",
                    source_attachment_id=attachment.id,
                    storage_key=storage_key,
                    content_type=attachment.content_type[:128] or "application/octet-stream",
                    byte_size=len(attachment.content),
                    stored_at=datetime.now(UTC),
                )
            )
        return stored_keys

    def _store_unassigned_attachments(
        self,
        *,
        email: HubMailboxEmail,
        attachments: tuple[_ParsedAttachment, ...],
    ) -> list[str]:
        stored_keys: list[str] = []
        for attachment in attachments:
            storage_key = self.communications.attachment_storage.store(attachment.content)
            stored_keys.append(storage_key)
            self.db.add(
                HubMailboxAttachment(
                    email_id=email.id,
                    source="mittwald-imap",
                    source_attachment_id=attachment.id,
                    storage_key=storage_key,
                    content_type=attachment.content_type[:128] or "application/octet-stream",
                    byte_size=len(attachment.content),
                    stored_at=datetime.now(UTC),
                )
            )
        return stored_keys

    def _matched_customers(self, payload: dict[str, object]) -> list[Customer]:
        direction = payload.get("direction")
        matching_values = payload.get("from") if direction == "inbound" else [payload.get("to"), payload.get("cc")]
        addresses = set()
        for value in matching_values if isinstance(matching_values, list) else [matching_values]:
            addresses.update(self._addresses(value))
        if not addresses:
            for key in ("from", "to", "cc"):
                addresses.update(self._addresses(payload.get(key)))
        if not addresses:
            return []

        customers: list[Customer] = []
        for customer in self.db.scalars(select(Customer).order_by(Customer.id.asc())).all():
            customer_addresses = {recipient.email for recipient in self.communications._recipients_for_customer(customer)}
            if customer_addresses.intersection(addresses):
                customers.append(customer)
        return customers

    def _list_uids(self, *, account: HubMailboxAccount, folder: str, since_date: date) -> tuple[str, ...]:
        with self._connected_imap(account) as imap:
            status, _data = imap.select(folder, readonly=True)
            if status != "OK":
                raise HubMailboxImapImportError(f"Der IMAP-Ordner {folder} konnte nicht geöffnet werden.")
            status, data = imap.uid("SEARCH", None, "SINCE", since_date.strftime("%d-%b-%Y"))
            if status != "OK":
                raise HubMailboxImapImportError(f"Der IMAP-Ordner {folder} konnte nicht durchsucht werden.")
        raw_uids = data[0] if data else b""
        if isinstance(raw_uids, bytes):
            values = raw_uids.decode("ascii", errors="ignore").split()
        else:
            values = str(raw_uids or "").split()
        return tuple(uid for uid in values if uid.isdigit())

    def _fetch_message(self, *, account: HubMailboxAccount, folder: str, uid: str) -> tuple[bytes, str]:
        with self._connected_imap(account) as imap:
            status, _data = imap.select(folder, readonly=True)
            if status != "OK":
                raise HubMailboxImapImportError(f"Der IMAP-Ordner {folder} konnte nicht geöffnet werden.")
            status, data = imap.uid("FETCH", uid, "(RFC822 FLAGS)")
            if status != "OK" or not data:
                raise HubMailboxImapImportError("Die ausgewählte E-Mail ist im Mittwald-Postfach nicht mehr verfügbar.")
        raw_message = next(
            (part[1] for part in data if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes)),
            None,
        )
        if not raw_message:
            raise HubMailboxImapImportError("Mittwald hat für die ausgewählte E-Mail keinen Inhalt geliefert.")
        if len(raw_message) > _MAX_MESSAGE_BYTES:
            raise HubMailboxImapImportError("Eine E-Mail ist größer als 100 MB und wurde nicht importiert.")
        response = b" ".join(part[0] for part in data if isinstance(part, tuple) and isinstance(part[0], bytes))
        return raw_message, response.decode("ascii", errors="ignore")

    @contextmanager
    def _connected_imap(self, account: HubMailboxAccount) -> Iterator[imaplib.IMAP4_SSL]:
        imap = None
        try:
            password = self.cipher.decrypt(account.encrypted_password)
            imap = imaplib.IMAP4_SSL(
                account.imap_host,
                account.imap_port,
                ssl_context=ssl.create_default_context(),
                timeout=_CONNECTION_TIMEOUT_SECONDS,
            )
            imap.login(account.username, password)
            yield imap
        except imaplib.IMAP4.error as exc:
            raise HubMailboxImapImportError("Die IMAP-Anmeldung bei Mittwald wurde abgelehnt.") from exc
        except (OSError, ValueError) as exc:
            raise HubMailboxImapImportError("Das Mittwald-Postfach ist momentan nicht erreichbar.") from exc
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except (OSError, imaplib.IMAP4.error):
                    pass

    def _parse_message(
        self,
        *,
        raw_message: bytes,
        account: HubMailboxAccount,
        folder: str,
        uid: str,
        flags: str,
    ) -> _ParsedMessage:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        direction = "inbound" if folder == "INBOX" else "outbound"
        identity = self._identity(
            message_id=str(message.get("Message-ID") or ""),
            raw_message=raw_message,
            account_id=account.id,
            folder=folder,
            uid=uid,
        )
        occurred_at = self._occurred_at(message)
        attachments, html_body, text_body = self._message_parts(message=message, identity=identity)
        people = lambda header: self._people(message.get_all(header, []))
        payload: dict[str, object] = {
            "subject": self._header_text(message.get("Subject")) or "Ohne Betreff",
            "from": people("From"),
            "to": people("To"),
            "cc": people("Cc"),
            "content": html_body if html_body is not None else text_body,
            "attachments": [{"id": attachment.id, "name": attachment.filename} for attachment in attachments],
            "direction": direction,
            "received_time": occurred_at.isoformat(),
            "mittwald_message_id": identity,
            "mittwald_mailbox": account.email_address,
        }
        return _ParsedMessage(
            identity=identity,
            payload=payload,
            occurred_at=occurred_at,
            is_unread=direction == "inbound" and "\\Seen" not in flags,
            attachments=attachments,
        )

    @classmethod
    def _message_parts(cls, *, message: Message, identity: str) -> tuple[tuple[_ParsedAttachment, ...], str | None, str | None]:
        attachments: list[_ParsedAttachment] = []
        html_body: str | None = None
        text_body: str | None = None
        for index, part in enumerate(message.walk()):
            if part.is_multipart():
                continue
            filename = cls._safe_filename(part.get_filename())
            disposition = str(part.get_content_disposition() or "").casefold()
            if filename or disposition == "attachment":
                content = part.get_payload(decode=True) or b""
                if not content:
                    continue
                attachment_id = sha256(f"{identity}:{index}:{filename}".encode("utf-8")).hexdigest()
                attachments.append(
                    _ParsedAttachment(
                        id=attachment_id,
                        filename=filename or f"Anhang-{index}",
                        content_type=str(part.get_content_type() or "application/octet-stream"),
                        content=content,
                    )
                )
                continue
            content_type = str(part.get_content_type() or "").casefold()
            if content_type not in {"text/html", "text/plain"}:
                continue
            content = cls._decode_part(part)
            if not content:
                continue
            if content_type == "text/html" and html_body is None:
                html_body = content
            elif content_type == "text/plain" and text_body is None:
                text_body = content
        return tuple(attachments), html_body, text_body

    @staticmethod
    def _decode_part(part: Message) -> str | None:
        raw = part.get_payload(decode=True)
        if raw is None:
            return None
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace").strip() or None
        except (LookupError, UnicodeError):
            return raw.decode("utf-8", errors="replace").strip() or None

    @staticmethod
    def _safe_filename(value: object) -> str:
        name = str(value or "").replace("\\", "/").split("/")[-1].strip()
        return name[:255]

    @staticmethod
    def _identity(*, message_id: str, raw_message: bytes, account_id: int, folder: str, uid: str) -> str:
        normalized = message_id.strip().replace("\r", "").replace("\n", "")
        if normalized and len(normalized) <= 500:
            return normalized
        digest = sha256(raw_message).hexdigest()
        return f"mittwald:{account_id}:{folder}:{uid}:{digest}"[:512]

    @staticmethod
    def _occurred_at(message: Message) -> datetime:
        try:
            parsed = parsedate_to_datetime(str(message.get("Date") or ""))
            if parsed is not None:
                return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError, IndexError):
            pass
        return datetime.now(UTC)

    @staticmethod
    def _header_text(value: object) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:2_000] or None

    @classmethod
    def _people(cls, values: list[object]) -> list[dict[str, str]]:
        people: list[dict[str, str]] = []
        seen: set[str] = set()
        for name, address in getaddresses([str(value) for value in values]):
            email_address = address.strip().casefold()
            if not email_address or "@" not in email_address or email_address in seen:
                continue
            seen.add(email_address)
            people.append({"name": cls._header_text(name) or email_address, "email": email_address})
        return people

    @classmethod
    def _addresses(cls, value: object) -> set[str]:
        if isinstance(value, list):
            return {str(item.get("email", "")).strip().casefold() for item in value if isinstance(item, dict) and "@" in str(item.get("email", ""))}
        if isinstance(value, dict):
            address = str(value.get("email", "")).strip().casefold()
            return {address} if "@" in address else set()
        return {address.strip().casefold() for _name, address in getaddresses([str(value or "")]) if "@" in address}

    def _record_failure(self, *, import_id: int, item_id: int, error: str) -> str:
        mailbox_import = self.db.get(HubMailboxImapImport, import_id)
        item = self.db.get(HubMailboxImapImportItem, item_id)
        if mailbox_import is None or item is None:
            self.db.rollback()
            return "failed"
        item.status = "failed"
        item.last_error = error[:1_000]
        mailbox_import.processed_messages += 1
        mailbox_import.failed_messages += 1
        mailbox_import.consecutive_failures += 1
        if mailbox_import.consecutive_failures >= 3:
            mailbox_import.status = "stopped"
            mailbox_import.completed_at = datetime.now(UTC)
            mailbox_import.last_error = "Import nach drei aufeinanderfolgenden Fehlern automatisch angehalten."
            outcome = "stopped"
        else:
            mailbox_import.last_error = error[:1_000]
            outcome = "failed"
        self.db.commit()
        return outcome

    def _active_import(self) -> HubMailboxImapImport | None:
        return self.db.scalar(
            select(HubMailboxImapImport)
            .where(HubMailboxImapImport.status.in_(_ACTIVE_STATUSES))
            .order_by(HubMailboxImapImport.id.asc())
            .limit(1)
        )

    @staticmethod
    def _status(mailbox_import: HubMailboxImapImport) -> HubMailboxImapImportStatus:
        return HubMailboxImapImportStatus(
            id=mailbox_import.id,
            since_date=mailbox_import.since_date,
            status=mailbox_import.status,
            total_messages=mailbox_import.total_messages,
            processed_messages=mailbox_import.processed_messages,
            imported_messages=mailbox_import.imported_messages,
            skipped_messages=mailbox_import.skipped_messages,
            failed_messages=mailbox_import.failed_messages,
            stored_attachments=mailbox_import.stored_attachments,
            stored_bytes=mailbox_import.stored_bytes,
            started_at=mailbox_import.started_at,
            completed_at=mailbox_import.completed_at,
            last_error=mailbox_import.last_error,
        )
