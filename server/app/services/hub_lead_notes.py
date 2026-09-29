"""Encrypted Lead note presentation and one-time Zoho import support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.services.hub_record_info import record_author
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
from app.services.hub_note_catalog import normalize_note


@dataclass(frozen=True)
class HubLeadNoteView:
    id: int
    title: str
    content: str
    author: str | None
    occurred_at: datetime | None


class HubLeadNoteError(ValueError):
    """A safe validation message for Lead note operations."""


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
        values = self._validated_note(title=title, content=content, creating=True)
        normalized_title, normalized_content = values["title"], values["content"]
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
        values = self._validated_note(title=title, content=content, creating=True)
        normalized_title, normalized_content = values["title"], values["content"]
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
        values = self._validated_note(title=title, content=content)
        payload = self._payload(note.encrypted_payload_json)
        payload.update(
            {
                "Note_Title": values["title"],
                "Note_Content": values["content"],
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

    @staticmethod
    def _validated_note(**values):
        try:
            return normalize_note(**values)
        except ValueError as exc:
            raise HubLeadNoteError(str(exc)) from exc

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
        title = self._text(payload.get("Note_Title")) or ""
        content = self._text(payload.get("Note_Content")) or ""
        return HubLeadNoteView(
            id=note.id,
            title=title,
            content=content,
            author=record_author(note, note.created_by_username),
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
    def _text(value: object) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        text = str(value).strip()
        return text or None
