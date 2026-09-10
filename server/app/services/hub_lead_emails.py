"""Encrypted Lead email presentation and one-time Zoho import support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_lead import HubLead
from app.models.hub_lead_email import HubLeadEmail
from app.services.customer_communications import CustomerCommunicationService
from app.services.zoho_crm import ZOHO_LEAD_MODULE, ZohoCrmError, ZohoCrmService


@dataclass(frozen=True)
class HubLeadEmailView:
    id: int
    subject: str
    sender: str | None
    recipients: str | None
    direction: str
    occurred_at: datetime | None
    preview_html: str | None


@dataclass(frozen=True)
class ZohoLeadEmailImportResult:
    checked_leads: int
    checked_headers: int
    imported_emails: int
    retained_emails: int
    skipped_before_cutoff: int
    skipped_without_timestamp: int
    failed_emails: int
    failed_leads: int


class HubLeadEmailService:
    """Read Lead email bodies without exposing encrypted storage to templates."""

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_email_views(self, *, lead_id: int) -> tuple[HubLeadEmailView, ...]:
        emails = self.db.scalars(
            select(HubLeadEmail)
            .where(HubLeadEmail.lead_id == lead_id)
            .order_by(HubLeadEmail.zoho_sent_at.desc(), HubLeadEmail.id.desc())
        ).all()
        return tuple(self._view(email) for email in emails)

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
            preview_html=CustomerCommunicationService._email_preview_document(content),
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


class ZohoLeadEmailImportService:
    """Import recent, complete Zoho Lead email bodies once into Hub storage."""

    _LOOKBACK = timedelta(days=365)
    _COMMIT_BATCH_SIZE = 50

    def __init__(self, *, db: Session, cipher: SecretCipher, zoho_service: ZohoCrmService):
        self.db = db
        self.cipher = cipher
        self.zoho_service = zoho_service

    def import_recent_emails(self, *, now: datetime | None = None) -> ZohoLeadEmailImportResult:
        imported_at = self._as_utc(now or datetime.now(UTC))
        cutoff = imported_at - self._LOOKBACK
        leads = self.db.scalars(
            select(HubLead).where(HubLead.zoho_id.is_not(None)).order_by(HubLead.id.asc())
        ).all()
        known = {
            (email.lead_id, email.zoho_message_id)
            for email in self.db.scalars(select(HubLeadEmail)).all()
        }
        counters = {
            "checked_leads": 0,
            "checked_headers": 0,
            "imported_emails": 0,
            "retained_emails": 0,
            "skipped_before_cutoff": 0,
            "skipped_without_timestamp": 0,
            "failed_emails": 0,
            "failed_leads": 0,
        }

        pending_in_batch = 0
        for lead in leads:
            if not lead.zoho_id:
                continue
            counters["checked_leads"] += 1
            try:
                headers = self.zoho_service.list_record_email_headers(ZOHO_LEAD_MODULE, lead.zoho_id)
            except ZohoCrmError:
                counters["failed_leads"] += 1
                continue

            for header in headers:
                counters["checked_headers"] += 1
                sent_at = self._email_datetime(header)
                if sent_at is None:
                    counters["skipped_without_timestamp"] += 1
                    continue
                if sent_at < cutoff:
                    counters["skipped_before_cutoff"] += 1
                    continue

                message_id = self._text(header.get("message_id")) or self._text(header.get("id"))
                if message_id is None:
                    counters["failed_emails"] += 1
                    continue
                if (lead.id, message_id) in known:
                    counters["retained_emails"] += 1
                    continue

                try:
                    body = self.zoho_service.get_record_email(
                        module=ZOHO_LEAD_MODULE,
                        record_id=lead.zoho_id,
                        message_id=message_id,
                        user_id=self._owner_id(header),
                    )
                    content = body.get("content")
                    if not isinstance(content, str):
                        raise ZohoCrmError("Zoho hat die Lead-E-Mail ohne Inhalt geliefert.")
                except (ValueError, ZohoCrmError):
                    counters["failed_emails"] += 1
                    continue

                payload = dict(header)
                payload.update(body)
                email = HubLeadEmail(
                    lead=lead,
                    zoho_message_id=message_id,
                    zoho_module=ZOHO_LEAD_MODULE,
                    zoho_record_id=lead.zoho_id,
                    direction=self._email_direction(payload),
                    encrypted_payload_json=self._encrypt_payload(payload),
                    encrypted_header_json=self._encrypt_header(payload),
                    zoho_sent_at=sent_at,
                    zoho_imported_at=imported_at,
                )
                self.db.add(email)
                known.add((lead.id, message_id))
                counters["imported_emails"] += 1
                pending_in_batch += 1
                if pending_in_batch >= self._COMMIT_BATCH_SIZE:
                    # The Zoho API can take a long time. Commit small batches so the
                    # database connection stays active and a retry resumes safely.
                    self.db.commit()
                    pending_in_batch = 0

        if pending_in_batch:
            self.db.commit()
        return ZohoLeadEmailImportResult(**counters)

    def _encrypt_payload(self, payload: dict[str, object]) -> str:
        return self.cipher.encrypt(json.dumps(payload, ensure_ascii=False, default=str))

    def _encrypt_header(self, payload: dict[str, object]) -> str:
        return self._encrypt_payload(
            {
                "subject": self._text(payload.get("subject")),
                "sender": CustomerCommunicationService._people_text(payload.get("from")),
                "recipients": CustomerCommunicationService._people_text(payload.get("to")),
            }
        )

    @classmethod
    def _email_datetime(cls, payload: dict[str, object]) -> datetime | None:
        for key in ("received_time", "sent_time", "time", "created_time"):
            value = payload.get(key)
            if not isinstance(value, str):
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return cls._as_utc(parsed)
        return None

    @staticmethod
    def _email_direction(payload: dict[str, object]) -> str:
        direction = ZohoLeadEmailImportService._text(payload.get("direction"))
        if direction in {"inbound", "outbound"}:
            return direction
        if payload.get("sent") is True:
            return "outbound"
        if payload.get("sent") is False:
            return "inbound"
        return "unknown"

    @classmethod
    def _owner_id(cls, payload: dict[str, object]) -> str | None:
        owner = payload.get("owner")
        return cls._text(owner.get("id")) if isinstance(owner, dict) else None

    @staticmethod
    def _text(value: object) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
