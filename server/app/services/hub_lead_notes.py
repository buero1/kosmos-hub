"""Encrypted Lead note presentation and one-time Zoho import support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

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


class HubLeadNoteError(ValueError):
    """A safe validation message for Lead note operations."""


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
    """Manage Lead notes without exposing plaintext payloads at rest."""

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

    def create_note(self, *, lead_id: int, actor: str, title: str = "", content: str) -> HubLeadNoteView:
        lead = self._lead_or_error(lead_id)
        normalized_content = self._required_text(content, "Notiz", maximum=30_000)
        normalized_title = (
            self._required_text(title, "Titel", maximum=255)
            if title.strip()
            else self._title_from_content(normalized_content)
        )
        now = datetime.now(UTC)
        note = HubLeadNote(
            lead=lead,
            zoho_note_id=f"hub-{uuid4().hex}",
            encrypted_payload_json=self._encrypt_payload(
                {"Note_Title": normalized_title, "Note_Content": normalized_content, "source": "hub"}
            ),
            created_by_username=self._required_text(actor, "Benutzer", maximum=128),
            zoho_created_at=now,
            zoho_modified_at=now,
            zoho_imported_at=now,
        )
        self.db.add(note)
        self.db.flush()
        return self._view(note)

    def upsert_external_note(
        self,
        *,
        lead_id: int,
        source_system: str,
        source_external_id: str,
        actor: str,
        title: str,
        content: str,
        occurred_at: datetime | None = None,
    ) -> HubLeadNoteView:
        self._lead_or_error(lead_id)
        normalized_source = self._required_text(source_system, "Externe Quelle", maximum=96)
        normalized_external_id = self._required_text(source_external_id, "Externe Notiz-ID", maximum=255)
        normalized_content = self._required_text(content, "Notiz", maximum=30_000)
        normalized_title = (
            self._required_text(title, "Titel", maximum=255)
            if title.strip()
            else self._title_from_content(normalized_content)
        )
        note = self.db.scalar(
            select(HubLeadNote).where(
                HubLeadNote.lead_id == lead_id,
                HubLeadNote.source_system == normalized_source,
                HubLeadNote.source_external_id == normalized_external_id,
            )
        )
        now = datetime.now(UTC)
        payload = {
            "Note_Title": normalized_title,
            "Note_Content": normalized_content,
            "source": normalized_source,
            "source_external_id": normalized_external_id,
        }
        if note is None:
            note = HubLeadNote(
                lead_id=lead_id,
                zoho_note_id=f"hub-{uuid4().hex}",
                source_system=normalized_source,
                source_external_id=normalized_external_id,
                encrypted_payload_json=self._encrypt_payload(payload),
                created_by_username=self._required_text(actor, "Benutzer", maximum=128),
                zoho_created_at=occurred_at or now,
                zoho_modified_at=now,
                zoho_imported_at=now,
            )
            self.db.add(note)
        else:
            note.encrypted_payload_json = self._encrypt_payload(payload)
            note.created_by_username = self._required_text(actor, "Benutzer", maximum=128)
            note.zoho_modified_at = now
            note.last_error = None
        self.db.flush()
        return self._view(note)

    def update_note(self, *, lead_id: int, note_id: int, title: str, content: str) -> HubLeadNoteView:
        note = self._note_or_error(lead_id=lead_id, note_id=note_id)
        payload = self._payload(note.encrypted_payload_json)
        payload.update(
            {
                "Note_Title": self._required_text(title, "Titel", maximum=255),
                "Note_Content": self._required_text(content, "Notiz", maximum=30_000),
            }
        )
        note.encrypted_payload_json = self._encrypt_payload(payload)
        note.zoho_modified_at = datetime.now(UTC)
        note.last_error = None
        self.db.flush()
        return self._view(note)

    def delete_note(self, *, lead_id: int, note_id: int) -> None:
        note = self._note_or_error(lead_id=lead_id, note_id=note_id)
        self.db.delete(note)
        self.db.flush()

    def _lead_or_error(self, lead_id: int) -> HubLead:
        lead = self.db.get(HubLead, lead_id)
        if lead is None:
            raise HubLeadNoteError("Lead wurde nicht gefunden.")
        return lead

    def _note_or_error(self, *, lead_id: int, note_id: int) -> HubLeadNote:
        note = self.db.scalar(
            select(HubLeadNote).where(HubLeadNote.id == note_id, HubLeadNote.lead_id == lead_id)
        )
        if note is None:
            raise HubLeadNoteError("Notiz wurde nicht gefunden.")
        return note

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

    def _encrypt_payload(self, payload: dict[str, object]) -> str:
        return self.cipher.encrypt(json.dumps(payload, ensure_ascii=False, default=str))

    @staticmethod
    def _required_text(value: str, label: str, *, maximum: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise HubLeadNoteError(f"{label} darf nicht leer sein.")
        if len(normalized) > maximum:
            raise HubLeadNoteError(f"{label} darf höchstens {maximum:,} Zeichen enthalten.")
        return normalized

    @staticmethod
    def _title_from_content(content: str) -> str:
        return next(line.strip() for line in content.splitlines() if line.strip())[:255]

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
