from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
import pytest

from app.services.hub_lead_notes import HubLeadNoteError, HubLeadNoteService






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
