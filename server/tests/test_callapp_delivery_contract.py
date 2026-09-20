from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.routes import integrations
from app.core.security import SecretCipher
from app.core.timezones import format_berlin_time
from app.db.base import Base
from app.models.customer_activity import CustomerCallActivity
from app.models.hub_lead_note import HubLeadNote
from app.services import hub_lead_notes
from app.services.hub_lead_notes import HubLeadNoteService


def test_delivery_creates_combined_note_now_and_only_planned_call(monkeypatch):
    current_time = datetime(2026, 9, 20, 11, 30, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time.astimezone(tz)

    monkeypatch.setattr(hub_lead_notes, "datetime", FrozenDatetime)
    cipher = SecretCipher("a" * 32)
    monkeypatch.setattr(integrations, "get_secret_cipher", lambda: cipher)
    monkeypatch.setattr(integrations, "get_settings", lambda: SimpleNamespace(public_base_url="https://hub.example"))
    monkeypatch.setattr(integrations, "_integration_actor", lambda _: ("integration:callapp:test", None))
    monkeypatch.setattr(integrations, "write_audit_log", lambda *args, **kwargs: None)
    payload = integrations.CallAppClosurePayload.model_validate({
        "instance_id": "test", "closure_id": "lead-1:call-1",
        "occurred_at": "2026-09-18T12:20:55+02:00",
        "notes": " System text ", "manual_note": " Manual text\nSecond line ",
        "lead": {"id": "lead-1", "company": "Example"}, "campaign": {"id": "campaign-1"},
        # Even old senders must not create a completed call.
        "call": {"id": "call-1", "started_at": "2026-09-18T12:20:55+02:00"},
        "follow_up": {"starts_at": "2026-09-21T09:00:00+02:00"},
    })
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        result = integrations.receive_closure(payload, None, db)
        assert result["created"] is True
        assert result["call_id"] is None
        db.expire_all()
        note = db.scalar(select(HubLeadNote))
        assert note is not None
        note_created_at = note.zoho_created_at
        assert format_berlin_time(note_created_at) == "20.09.2026 13:30:00 CEST"
        view = HubLeadNoteService(db=db, cipher=cipher)._view(note)
        assert view.content == "System Kommentar:\nSystem text\n\nManuelle Notiz:\nManual text\nSecond line"
        calls = db.scalars(select(CustomerCallActivity)).all()
        assert len(calls) == 1
        assert calls[0].status == "planned"
        assert calls[0].description == "Manual text\nSecond line"
        assert format_berlin_time(calls[0].starts_at) == "21.09.2026 09:00:00 CEST"

        # Resending updates content, preserving the original Hub creation time and IDs.
        current_time = datetime(2026, 9, 22, 11, 30, tzinfo=UTC)
        payload.manual_note = "Updated manual text"
        result_again = integrations.receive_closure(payload, None, db)
        db.expire_all()
        assert result_again["created"] is False
        assert result_again["note_id"] == result["note_id"]
        assert result_again["follow_up_id"] == result["follow_up_id"]
        assert len(db.scalars(select(HubLeadNote)).all()) == 1
        assert len(db.scalars(select(CustomerCallActivity)).all()) == 1
        note = db.scalar(select(HubLeadNote))
        assert note.zoho_created_at == note_created_at
        assert "Manuelle Notiz:\nUpdated manual text" in HubLeadNoteService(db=db, cipher=cipher)._view(note).content
        assert db.scalar(select(CustomerCallActivity)).description == "Updated manual text"


def test_manual_note_without_comment_or_follow_up_is_saved(monkeypatch):
    cipher = SecretCipher("a" * 32)
    monkeypatch.setattr(integrations, "get_secret_cipher", lambda: cipher)
    monkeypatch.setattr(integrations, "get_settings", lambda: SimpleNamespace(public_base_url="https://hub.example"))
    monkeypatch.setattr(integrations, "_integration_actor", lambda _: ("integration:callapp:test", None))
    monkeypatch.setattr(integrations, "write_audit_log", lambda *args, **kwargs: None)
    payload = integrations.CallAppClosurePayload.model_validate({
        "closure_id": "manual-only", "occurred_at": "2026-09-18T12:20:55+02:00",
        "manual_note": "Manual only", "lead": {"id": "manual-only"}, "campaign": {"id": "campaign"},
    })
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        result = integrations.receive_closure(payload, None, db)
        assert result["follow_up_id"] is None
        assert db.scalar(select(CustomerCallActivity)) is None
        view = HubLeadNoteService(db=db, cipher=cipher)._view(db.scalar(select(HubLeadNote)))
        assert view.content == "Manuelle Notiz:\nManual only"
