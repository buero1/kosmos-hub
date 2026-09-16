from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
import pytest

from app.services.hub_lead_notes import HubLeadNoteError, HubLeadNoteService, ZohoLeadNoteImportService


class FakeZohoLeadNoteService:
    def list_record_notes(self, module: str, record_id: str) -> list[dict[str, object]]:
        assert module == "Leads"
        assert record_id == "zoho-lead-1"
        return [
            {
                "id": "recent-note",
                "Note_Title": "Aktuelle Abstimmung",
                "Note_Content": "Der Entwurf wird diese Woche versendet.",
                "Created_By": {"name": "Erika Beispiel"},
                "Created_Time": "2026-09-09T10:00:00+02:00",
                "Modified_Time": "2026-09-09T12:00:00+02:00",
            },
            {
                "id": "old-note",
                "Note_Title": "Alte Abstimmung",
                "Created_Time": "2025-09-09T10:00:00+02:00",
            },
            {"id": "without-time", "Note_Title": "Ohne Datum"},
        ]


def test_imports_only_recent_lead_notes_with_encrypted_content_and_is_repeatable():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        lead = HubLead(zoho_id="zoho-lead-1", encrypted_profile_json=cipher.encrypt('{"fields": {}}'))
        db.add(lead)
        db.commit()
        importer = ZohoLeadNoteImportService(
            db=db,
            cipher=cipher,
            zoho_service=FakeZohoLeadNoteService(),  # type: ignore[arg-type]
        )
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

        result = importer.import_recent_notes(now=now)
        assert result.checked_leads == 1
        assert result.checked_notes == 3
        assert result.imported_notes == 1
        assert result.skipped_before_cutoff == 1
        assert result.skipped_without_timestamp == 1

        note = db.scalar(select(HubLeadNote))
        assert note is not None
        assert "Aktuelle Abstimmung" not in note.encrypted_payload_json
        assert "Entwurf wird" not in note.encrypted_payload_json
        views = HubLeadNoteService(db=db, cipher=cipher).list_note_views(lead_id=lead.id)
        assert len(views) == 1
        assert views[0].title == "Aktuelle Abstimmung"
        assert views[0].content == "Der Entwurf wird diese Woche versendet."
        assert views[0].author == "Erika Beispiel"

        repeat = importer.import_recent_notes(now=now)
        assert repeat.imported_notes == 0
        assert repeat.retained_notes == 1


def test_hub_lead_notes_can_be_created_updated_and_deleted_locally():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        lead = HubLead(zoho_id=None, encrypted_profile_json=cipher.encrypt('{"fields": {}}'))
        other_lead = HubLead(zoho_id=None, encrypted_profile_json=cipher.encrypt('{"fields": {}}'))
        db.add_all([lead, other_lead])
        db.flush()
        service = HubLeadNoteService(db=db, cipher=cipher)

        created = service.create_note(lead_id=lead.id, actor="hub-admin", content="Erste Zeile\nWeitere Details")
        stored = db.get(HubLeadNote, created.id)
        assert stored is not None
        assert stored.zoho_note_id.startswith("hub-")
        assert "Erste Zeile" not in stored.encrypted_payload_json
        assert created.title == "Erste Zeile"
        assert created.author == "hub-admin"

        updated = service.update_note(lead_id=lead.id, note_id=created.id, title="Neuer Titel", content="Geändert")
        assert updated.title == "Neuer Titel"
        assert updated.content == "Geändert"
        with pytest.raises(HubLeadNoteError, match="nicht gefunden"):
            service.update_note(lead_id=other_lead.id, note_id=created.id, title="Falsch", content="Falsch")

        service.delete_note(lead_id=lead.id, note_id=created.id)
        assert service.list_note_views(lead_id=lead.id) == ()


def test_hub_lead_note_rejects_blank_content():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        lead = HubLead(zoho_id=None, encrypted_profile_json=cipher.encrypt('{"fields": {}}'))
        db.add(lead)
        db.flush()

        with pytest.raises(HubLeadNoteError, match="Notiz darf nicht leer sein"):
            HubLeadNoteService(db=db, cipher=cipher).create_note(lead_id=lead.id, actor="hub-admin", content="   ")
