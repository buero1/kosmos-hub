"""Unified mailbox views for linked customer mail and unassigned Zoho workflow mail."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only, selectinload

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.customer_communications import CustomerCommunicationAttachment, CustomerCommunicationService


MAILBOX_FOLDERS = frozenset({"inbox", "sent", "unassigned"})
MAILBOX_VIRTUAL_PAGE_SIZE = 100
MAILBOX_VIRTUAL_ROW_HEIGHT = 92


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
    direction: str
    is_unread: bool
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
    occurred_at: datetime | None
    customers: tuple[HubMailboxCustomerLink, ...]


@dataclass(frozen=True)
class HubMailboxListPage:
    messages: tuple[HubMailboxListItem, ...]
    total_count: int
    offset: int
    row_height: int = MAILBOX_VIRTUAL_ROW_HEIGHT


@dataclass(frozen=True)
class HubMailboxView:
    messages: tuple[HubMailboxListItem, ...]
    selected: HubMailboxMessage | None
    folder_counts: dict[str, int]
    total_count: int
    offset: int
    row_height: int = MAILBOX_VIRTUAL_ROW_HEIGHT


@dataclass(frozen=True)
class _MailboxListSource:
    key: str
    kind: str
    direction: str
    is_unread: bool
    occurred_at: datetime | None
    linked_emails: tuple[CustomerZohoEmail, ...] = ()
    unassigned_email: HubMailboxEmail | None = None


class HubMailboxService:
    """Present one mailbox while keeping customer email storage and permissions intact."""

    def __init__(self, *, db: Session, cipher: SecretCipher, public_base_url: str) -> None:
        self.db = db
        self.cipher = cipher
        self.communications = CustomerCommunicationService(
            db=db,
            cipher=cipher,
            public_base_url=public_base_url,
        )

    def get_view(self, *, folder: str, unread_only: bool, selected_key: str = "") -> HubMailboxView:
        if folder not in MAILBOX_FOLDERS:
            raise ValueError("Unbekannter E-Mail-Ordner.")

        sources = self._list_sources()
        folder_counts = {
            "inbox": sum(source.direction == "inbound" for source in sources),
            "sent": sum(source.direction == "outbound" for source in sources),
            "unassigned": sum(source.kind == "unassigned" for source in sources),
        }
        sources = self._filter_sources(sources, folder=folder, unread_only=unread_only)
        return self._view_from_sources(
            sources=sources,
            folder=folder,
            unread_only=unread_only,
            selected_key=selected_key,
            folder_counts=folder_counts,
        )

    def get_folder_view(self, *, folder: str, unread_only: bool, selected_key: str = "") -> HubMailboxView:
        """Load one folder for in-page navigation without rebuilding the other folders."""
        if folder not in MAILBOX_FOLDERS:
            raise ValueError("Unbekannter E-Mail-Ordner.")

        return self._view_from_sources(
            sources=self._folder_sources(folder=folder, unread_only=unread_only),
            folder=folder,
            unread_only=unread_only,
            selected_key=selected_key,
            folder_counts={},
        )

    def get_list_page(
        self,
        *,
        folder: str,
        unread_only: bool,
        offset: int,
    ) -> HubMailboxListPage:
        """Fetch a thin, scrollable mailbox segment without loading email content."""
        if folder not in MAILBOX_FOLDERS:
            raise ValueError("Unbekannter E-Mail-Ordner.")

        sources = self._folder_sources(folder=folder, unread_only=unread_only)
        safe_offset = max(0, min(offset, len(sources)))
        page_sources = sources[safe_offset : safe_offset + MAILBOX_VIRTUAL_PAGE_SIZE]
        return HubMailboxListPage(
            messages=self._list_items(page_sources),
            total_count=len(sources),
            offset=safe_offset,
        )

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

    def _view_from_sources(
        self,
        *,
        sources: list[_MailboxListSource],
        folder: str,
        unread_only: bool,
        selected_key: str,
        folder_counts: dict[str, int],
    ) -> HubMailboxView:
        selected_index = next((index for index, source in enumerate(sources) if source.key == selected_key), 0)
        offset = (selected_index // MAILBOX_VIRTUAL_PAGE_SIZE) * MAILBOX_VIRTUAL_PAGE_SIZE if sources else 0
        page_sources = sources[offset : offset + MAILBOX_VIRTUAL_PAGE_SIZE]
        messages = self._list_items(page_sources)
        selected_item = next((message for message in messages if message.key == selected_key), messages[0] if messages else None)
        selected = self.get_selected_message(
            folder=folder,
            unread_only=unread_only,
            selected_key=selected_item.key,
        ) if selected_item else None
        return HubMailboxView(
            messages=messages,
            selected=selected,
            folder_counts=folder_counts,
            total_count=len(sources),
            offset=offset,
        )

    def _list_sources(self, *, direction: str | None = None) -> list[_MailboxListSource]:
        sources = self._linked_list_sources(direction=direction) + self._unassigned_list_sources(direction=direction)
        return self._sort_sources(sources)

    def _folder_sources(self, *, folder: str, unread_only: bool) -> list[_MailboxListSource]:
        if folder == "unassigned":
            sources = self._unassigned_list_sources()
        else:
            direction = "inbound" if folder == "inbox" else "outbound"
            sources = self._list_sources(direction=direction)
        return self._filter_sources(sources, folder=folder, unread_only=unread_only)

    def _linked_list_sources(self, *, direction: str | None = None) -> list[_MailboxListSource]:
        """Read list metadata first; encrypted headers are fetched only for the requested rows."""
        statement = (
            select(CustomerZohoEmail)
            .options(
                load_only(
                    CustomerZohoEmail.id,
                    CustomerZohoEmail.customer_id,
                    CustomerZohoEmail.zoho_message_id,
                    CustomerZohoEmail.direction,
                    CustomerZohoEmail.is_unread,
                    CustomerZohoEmail.zoho_sent_at,
                    CustomerZohoEmail.created_at,
                ),
                selectinload(CustomerZohoEmail.customer).load_only(Customer.id, Customer.name),
            )
            .order_by(CustomerZohoEmail.zoho_sent_at.desc(), CustomerZohoEmail.id.desc())
        )
        if direction is not None:
            statement = statement.where(CustomerZohoEmail.direction == direction)
        rows = self.db.scalars(statement).all()
        groups: dict[str, list[CustomerZohoEmail]] = {}
        for email in rows:
            group_key = email.zoho_message_id or f"local-{email.id}"
            groups.setdefault(group_key, []).append(email)

        sources: list[_MailboxListSource] = []
        for emails in groups.values():
            winner = self._list_winner(emails)
            sources.append(
                _MailboxListSource(
                    key=f"linked-{winner.customer_id}-{winner.id}",
                    kind="linked",
                    direction=winner.direction,
                    is_unread=any(email.is_unread and email.direction == "inbound" for email in emails),
                    occurred_at=winner.zoho_sent_at or winner.created_at,
                    linked_emails=tuple(emails),
                )
            )
        return sources

    def _unassigned_list_sources(self, *, direction: str | None = None) -> list[_MailboxListSource]:
        statement = (
            select(HubMailboxEmail)
            .options(
                load_only(
                    HubMailboxEmail.id,
                    HubMailboxEmail.direction,
                    HubMailboxEmail.is_unread,
                    HubMailboxEmail.received_at,
                )
            )
            .order_by(HubMailboxEmail.received_at.desc(), HubMailboxEmail.id.desc())
        )
        if direction is not None:
            statement = statement.where(HubMailboxEmail.direction == direction)
        return [
            _MailboxListSource(
                key=f"unassigned-{email.id}",
                kind="unassigned",
                direction=email.direction,
                is_unread=email.is_unread,
                occurred_at=email.received_at,
                unassigned_email=email,
            )
            for email in self.db.scalars(statement).all()
        ]

    def _list_items(self, sources: list[_MailboxListSource]) -> tuple[HubMailboxListItem, ...]:
        linked_winners = [self._list_winner(list(source.linked_emails)) for source in sources if source.kind == "linked"]
        linked_payloads = dict(
            self.db.execute(
                select(CustomerZohoEmail.id, CustomerZohoEmail.encrypted_payload_json).where(
                    CustomerZohoEmail.id.in_([email.id for email in linked_winners])
                )
            ).all()
        ) if linked_winners else {}
        unassigned_emails = [source.unassigned_email for source in sources if source.unassigned_email is not None]
        unassigned_payloads = dict(
            self.db.execute(
                select(HubMailboxEmail.id, HubMailboxEmail.encrypted_payload_json).where(
                    HubMailboxEmail.id.in_([email.id for email in unassigned_emails])
                )
            ).all()
        ) if unassigned_emails else {}

        items: list[HubMailboxListItem] = []
        for source in sources:
            if source.kind == "linked":
                winner = self._list_winner(list(source.linked_emails))
                items.append(
                    self._linked_list_message(
                        list(source.linked_emails),
                        encrypted_payload_json=linked_payloads[winner.id],
                    )
                )
            elif source.unassigned_email is not None:
                items.append(
                    self._unassigned_list_message(
                        source.unassigned_email,
                        encrypted_payload_json=unassigned_payloads[source.unassigned_email.id],
                    )
                )
        return tuple(items)

    @staticmethod
    def _list_winner(emails: list[CustomerZohoEmail]) -> CustomerZohoEmail:
        return max(emails, key=lambda email: email.id)

    def _linked_list_message(
        self,
        emails: list[CustomerZohoEmail],
        *,
        encrypted_payload_json: str | None = None,
    ) -> HubMailboxListItem:
        # The full view may parse HTML and attachments. A list row only needs its header fields.
        winner = self._list_winner(emails)
        payload = self._payload(encrypted_payload_json if encrypted_payload_json is not None else winner.encrypted_payload_json)
        customers = self._linked_customers(emails)
        return HubMailboxListItem(
            key=f"linked-{winner.customer_id}-{winner.id}",
            kind="linked",
            subject=self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._people(payload.get("from")),
            recipients=self._people(payload.get("to")),
            direction=winner.direction,
            is_unread=any(email.is_unread and email.direction == "inbound" for email in emails),
            occurred_at=winner.zoho_sent_at or winner.created_at,
            customers=customers,
        )

    def _unassigned_list_message(
        self,
        email: HubMailboxEmail,
        *,
        encrypted_payload_json: str | None = None,
    ) -> HubMailboxListItem:
        payload = self._payload(encrypted_payload_json if encrypted_payload_json is not None else email.encrypted_payload_json)
        return HubMailboxListItem(
            key=f"unassigned-{email.id}",
            kind="unassigned",
            subject=self._text(payload.get("betreff")) or self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._people(payload.get("absender") or payload.get("sender") or payload.get("from")),
            recipients=self._people(payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient") or payload.get("to")),
            direction=email.direction,
            is_unread=email.is_unread,
            occurred_at=email.received_at,
            customers=(),
        )

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
            direction=view.direction,
            is_unread=any(email.is_unread and email.direction == "inbound" for email in emails),
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
        return HubMailboxMessage(
            key=f"unassigned-{email.id}",
            kind="unassigned",
            subject=self._text(payload.get("betreff")) or self._text(payload.get("subject")) or "Ohne Betreff",
            sender=self._people(payload.get("absender") or payload.get("sender") or payload.get("from")),
            recipients=self._people(payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient") or payload.get("to")),
            direction=email.direction,
            is_unread=email.is_unread,
            occurred_at=email.received_at,
            customers=(),
            customer_id=None,
            customer_email_id=email.id,
            # Deluge can send an unknown email body directly; render it only inside the sandboxed preview.
            preview_html=CustomerCommunicationService._email_preview_document(content),
            attachments=(),
            can_load_content=False,
            last_error=email.last_error,
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

    def _filter_sources(
        self,
        sources: list[_MailboxListSource],
        *,
        folder: str,
        unread_only: bool,
    ) -> list[_MailboxListSource]:
        filtered = [source for source in sources if self._matches_folder(source, folder)]
        if unread_only:
            filtered = [source for source in filtered if source.is_unread]
        return self._sort_sources(filtered)

    @staticmethod
    def _sort_sources(sources: list[_MailboxListSource]) -> list[_MailboxListSource]:
        return sorted(
            sources,
            key=lambda source: (source.occurred_at.timestamp() if source.occurred_at else 0, source.key),
            reverse=True,
        )

    @staticmethod
    def _matches_folder(message: HubMailboxMessage | HubMailboxListItem | _MailboxListSource, folder: str) -> bool:
        if folder == "unassigned":
            return message.kind == "unassigned"
        if folder == "sent":
            return message.direction == "outbound"
        return message.direction == "inbound"

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
