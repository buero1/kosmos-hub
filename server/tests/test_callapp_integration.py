from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer_activity import CustomerCallActivity
from app.models.hub_lead import HubLead
from app.models.hub_lead_note import HubLeadNote
from app.models.hub_user import HubUser
from app.services.customer_activities import CustomerActivityService
from app.services.hub_accounts import HubAccountService
from app.services.hub_lead_notes import HubLeadNoteService
from app.services.hub_leads import HubLeadService


def test_callapp_token_is_stored_as_digest_and_authenticates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = HubUser(username="admin", password_hash="hash", role="admin")
        db.add(user)
        db.flush()
        service = HubAccountService(db=db, app_secret_key="a" * 32)
        record, plain_token = service.create_integration_token(
            user=user,
            name="CallApp Produktion",
            source_key="callapp",
        )
        db.commit()

        assert plain_token.startswith("khint_")
        assert plain_token not in record.token_digest
        authenticated = service.authenticate_integration_token(plain_token)
        assert authenticated is not None
        authenticated_user, authenticated_token = authenticated
        assert authenticated_user.id == user.id
        assert authenticated_token.id == record.id


def test_callapp_entities_are_idempotently_upserted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        leads = HubLeadService(db=db, cipher=cipher)
        notes = HubLeadNoteService(db=db, cipher=cipher)
        activities = CustomerActivityService(db=db)
        lead, created = leads.upsert_external_lead(
            source_system="callapp:primary",
            source_external_id="lead-1",
            field_values={
                "first_name": "Erika",
                "last_name": "Beispiel",
                "company": "Beispiel GmbH",
                "email": "erika@example.org",
                "industry": "Gastronomie",
                "homepage": "keine",
                "source": "CallApp",
                "lead_status": "Lead erstellt",
                "lead_type": "Angebot vereinbart",
                "lead_result": "Offen",
                "condition": "Option 1",
            },
        )
        assert created is True
        notes.upsert_external_note(
            lead_id=lead.id,
            source_system="callapp:primary",
            source_external_id="closure-1",
            actor="integration:callapp:test",
            title="Gesprächsnotiz",
            content="Erstes Gespräch",
            occurred_at=datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
        )
        activities.upsert_external_call(
            lead_id=lead.id,
            source_system="callapp:primary",
            source_external_id="call-1",
            actor="integration:callapp:test",
            name="CallApp-Anruf",
            status="completed",
            direction="outbound",
            starts_at=datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
            duration_seconds=75,
            description="Erstes Gespräch",
            recording_url="https://callapp.example/calls/call-1/playback",
        )
        updated, created_again = leads.upsert_external_lead(
            source_system="callapp:primary",
            source_external_id="lead-1",
            field_values={"company": "Beispiel GmbH aktualisiert"},
        )
        notes.upsert_external_note(
            lead_id=updated.id,
            source_system="callapp:primary",
            source_external_id="closure-1",
            actor="integration:callapp:test",
            title="Gesprächsnotiz",
            content="Aktualisierte Notiz",
            occurred_at=datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
        )
        activities.upsert_external_call(
            lead_id=updated.id,
            source_system="callapp:primary",
            source_external_id="call-1",
            actor="integration:callapp:test",
            name="CallApp-Anruf",
            status="completed",
            direction="outbound",
            starts_at=datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
            duration_seconds=90,
            description="Aktualisiertes Gespräch",
            recording_url="https://callapp.example/calls/call-1/playback",
        )
        db.commit()

        assert created_again is False
        assert db.scalar(select(HubLead).where(HubLead.source_external_id == "lead-1")) is not None
        assert len(db.scalars(select(HubLeadNote)).all()) == 1
        stored_call = db.scalar(select(CustomerCallActivity).where(CustomerCallActivity.source_external_id == "call-1"))
        assert stored_call is not None
        assert stored_call.duration_seconds == 90
