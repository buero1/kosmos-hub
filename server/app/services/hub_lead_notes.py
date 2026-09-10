"""Encrypted Lead note presentation and one-time Zoho import support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
from app.services.zoho_crm import ZOHO_LEAD_MODULE, ZohoCrmError, ZohoCrmService


@dataclass(frozen=True)
class HubLeadNoteView:
    id: int
    title: str
    content: str
    author: str | None
    occurred_at: datetime | None


@dataclass(frozen=True)
class ZohoLeadNoteImportResult:
    checked_leads: int
    checked_notes: int
    imported_notes: int
    retained_notes: int
    skipped_before_cutoff: int
    skipped_without_timestamp: int
    failed_notes: int
    failed_leads: int


class HubLeadNoteService:
    """Read Lead note text without exposing encrypted payloads to templates."""

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_note_views(self, *, lead_id: int) -> tuple[HubLeadNoteView, ...]:
        notes = self.db.scalars(
            select(HubLeadNote)
            .where(HubLeadNote.lead_id == lead_id)
            .order_by(HubLeadNote.zoho_created_at.desc(), HubLeadNote.id.desc())
        ).all()
        return tuple(self._view(note) for note in notes)

    def _view(self, note: HubLeadNote) -> HubLeadNoteView:
        payload = self._payload(note.encrypted_payload_json)
        title = self._text(payload.get("Note_Title")) or "Ohne Titel"
        content = self._text(payload.get("Note_Content")) or ""
        return HubLeadNoteView(
            id=note.id,
            title=title,
            content=content,
            author=note.created_by_username,
            occurred_at=note.zoho_created_at or note.created_at,
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


class ZohoLeadNoteImportService:
    """Import recent Zoho Lead notes in durable, restart-safe batches."""

    _LOOKBACK = timedelta(days=365)
    _COMMIT_BATCH_SIZE = 50

    def __init__(self, *, db: Session, cipher: SecretCipher, zoho_service: ZohoCrmService):
        self.db = db
        self.cipher = cipher
        self.zoho_service = zoho_service

    def import_recent_notes(self, *, now: datetime | None = None) -> ZohoLeadNoteImportResult:
        imported_at = self._as_utc(now or datetime.now(UTC))
        cutoff = imported_at - self._LOOKBACK
        leads = self.db.scalars(
            select(HubLead).where(HubLead.zoho_id.is_not(None)).order_by(HubLead.id.asc())
        ).all()
        known = {
            (note.lead_id, note.zoho_note_id)
            for note in self.db.scalars(select(HubLeadNote)).all()
        }
        counters = {
            "checked_leads": 0,
            "checked_notes": 0,
            "imported_notes": 0,
            "retained_notes": 0,
            "skipped_before_cutoff": 0,
            "skipped_without_timestamp": 0,
            "failed_notes": 0,
            "failed_leads": 0,
        }

        pending_in_batch = 0
        for lead in leads:
            if not lead.zoho_id:
                continue
            counters["checked_leads"] += 1
            try:
                records = self.zoho_service.list_record_notes(ZOHO_LEAD_MODULE, lead.zoho_id)
            except ZohoCrmError:
                counters["failed_leads"] += 1
                continue

            for record in records:
                counters["checked_notes"] += 1
                created_at = self._created_at(record)
                if created_at is None:
                    counters["skipped_without_timestamp"] += 1
                    continue
                if created_at < cutoff:
                    counters["skipped_before_cutoff"] += 1
                    continue
                note_id = self._text(record.get("id"))
                if note_id is None:
                    counters["failed_notes"] += 1
                    continue
                if (lead.id, note_id) in known:
                    counters["retained_notes"] += 1
                    continue

                self.db.add(
                    HubLeadNote(
                        lead=lead,
                        zoho_note_id=note_id,
                        encrypted_payload_json=self._encrypt_payload(record),
                        created_by_username=self._creator_name(record.get("Created_By")),
                        zoho_created_at=created_at,
                        zoho_modified_at=self._datetime(record.get("Modified_Time")),
                        zoho_imported_at=imported_at,
                    )
                )
                known.add((lead.id, note_id))
                counters["imported_notes"] += 1
                pending_in_batch += 1
                if pending_in_batch >= self._COMMIT_BATCH_SIZE:
                    # A long Zoho import must not hold one idle database connection.
                    self.db.commit()
                    pending_in_batch = 0

        if pending_in_batch:
            self.db.commit()
        return ZohoLeadNoteImportResult(**counters)

    def _encrypt_payload(self, payload: dict[str, object]) -> str:
        return self.cipher.encrypt(json.dumps(payload, ensure_ascii=False, default=str))

    @classmethod
    def _created_at(cls, payload: dict[str, object]) -> datetime | None:
        return cls._datetime(payload.get("Created_Time"))

    @staticmethod
    def _creator_name(value: object) -> str | None:
        if isinstance(value, dict):
            name = value.get("name") or value.get("email")
            return ZohoLeadNoteImportService._text(name)
        return ZohoLeadNoteImportService._text(value)

    @staticmethod
    def _text(value: object) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _datetime(cls, value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return cls._as_utc(parsed)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
