"""Encrypted Lead email presentation and one-time Zoho import support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_lead_email import HubLeadEmail
from app.services.customer_communications import CustomerCommunicationService


@dataclass(frozen=True)
class HubLeadEmailView:
    id: int
    subject: str
    sender: str | None
    recipients: str | None
    direction: str
    occurred_at: datetime | None
    preview_html: str | None
    mailbox_message: object | None = None


class HubLeadEmailService:
    """Read Lead email bodies without exposing encrypted storage to templates."""

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_email_views(self, *, lead_id: int, actor: str | None = None) -> tuple[HubLeadEmailView, ...]:
        from app.core.config import get_settings
        from app.services.hub_mailbox import HubMailboxService
        mailbox = HubMailboxService(db=self.db, cipher=self.cipher, public_base_url=get_settings().public_base_url, actor=actor)
        if mailbox.scope is not None and not mailbox.scope.record_visible("leads", lead_id):
            return ()
        emails = self.db.scalars(
            select(HubLeadEmail)
            .where(HubLeadEmail.lead_id == lead_id)
            .order_by(HubLeadEmail.zoho_sent_at.desc(), HubLeadEmail.id.desc())
        ).all()
        emails = [email for email in emails if mailbox.scope is None or mailbox.scope.mailboxes.message_allowed(email)]
        views = [self._view(email) for email in emails]
        identities = {email.zoho_message_id for email in emails}
        for message in mailbox.related_messages(module="leads", record_id=lead_id):
            if message.message_id and message.message_id in identities:
                continue
            views.append(HubLeadEmailView(id=message.customer_email_id, subject=message.subject, sender=message.sender,
                recipients=message.recipients, direction=message.direction, occurred_at=message.occurred_at,
                preview_html=message.preview_html, mailbox_message=message))
        return tuple(sorted(views, key=lambda view: CustomerCommunicationService._aware_utc(view.occurred_at) if view.occurred_at else datetime.min.replace(tzinfo=UTC), reverse=True))

    def _view(self, email: HubLeadEmail) -> HubLeadEmailView:
        payload = self._payload(email.encrypted_payload_json)
        content_value = payload.get("content")
        content = content_value if isinstance(content_value, str) else None
        return HubLeadEmailView(
            id=email.id,
            subject=self._text(payload.get("subject")) or "Ohne Betreff",
            sender=CustomerCommunicationService._people_text(payload.get("from")),
            recipients=CustomerCommunicationService._people_text(payload.get("to")),
            direction=email.direction,
            occurred_at=email.zoho_sent_at or email.created_at,
            preview_html=CustomerCommunicationService._email_preview_document(
                content, content_type=self._text(payload.get("content_type")),
            ),
        )

    def _payload(self, encrypted_payload: str) -> dict[str, object]:
        try:
            payload = json.loads(self.cipher.decrypt(encrypted_payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _text(value: object) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        text = str(value).strip()
        return text or None
