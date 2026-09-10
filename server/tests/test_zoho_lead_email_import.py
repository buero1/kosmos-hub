from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_lead import HubLead
from app.models.hub_lead_email import HubLeadEmail
from app.services.hub_lead_emails import HubLeadEmailService, ZohoLeadEmailImportService


class FakeZohoLeadEmailService:
    def __init__(self) -> None:
        self.content_requests: list[tuple[str, str, str, str | None]] = []

    def list_record_email_headers(self, module: str, record_id: str) -> list[dict[str, object]]:
        assert module == "Leads"
        assert record_id == "zoho-lead-1"
        return [
            {
                "message_id": "recent-message",
                "subject": "Aktuelle Anfrage",
                "from": {"name": "Erika Beispiel", "email": "erika@beispiel.de"},
                "to": [{"name": "Kosmos", "email": "info@kosmos-medien.de"}],
                "owner": {"id": "zoho-owner-1"},
                "sent": False,
                "time": "2026-09-09T10:00:00+02:00",
            },
            {
                "message_id": "old-message",
                "subject": "Alte Anfrage",
                "time": "2025-09-09T10:00:00+02:00",
            },
            {
                "message_id": "without-time",
                "subject": "Ohne Datum",
            },
        ]

    def get_record_email(
        self,
        *,
        module: str,
        record_id: str,
        message_id: str,
        user_id: str | None = None,
    ) -> dict[str, object]:
        self.content_requests.append((module, record_id, message_id, user_id))
        assert message_id == "recent-message"
        return {"content": "<p>Vielen Dank für Ihre Anfrage.</p>"}


class BatchFakeZohoLeadEmailService(FakeZohoLeadEmailService):
    def list_record_email_headers(self, module: str, record_id: str) -> list[dict[str, object]]:
        return [
            {
                "message_id": "recent-message-1",
                "subject": "Erste Anfrage",
                "time": "2026-09-09T10:00:00+02:00",
            },
            {
                "message_id": "recent-message-2",
                "subject": "Zweite Anfrage",
                "time": "2026-09-09T11:00:00+02:00",
            },
        ]

    def get_record_email(self, **kwargs: object) -> dict[str, object]:
        return {"content": "<p>Vollständiger Inhalt.</p>"}


def test_imports_only_recent_lead_emails_with_encrypted_complete_content_and_is_repeatable():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        lead = HubLead(zoho_id="zoho-lead-1", encrypted_profile_json=cipher.encrypt('{"fields": {}}'))
        db.add(lead)
        db.flush()
        zoho = FakeZohoLeadEmailService()
        importer = ZohoLeadEmailImportService(db=db, cipher=cipher, zoho_service=zoho)  # type: ignore[arg-type]
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

        result = importer.import_recent_emails(now=now)
        db.commit()

        assert result.checked_leads == 1
        assert result.checked_headers == 3
        assert result.imported_emails == 1
        assert result.skipped_before_cutoff == 1
        assert result.skipped_without_timestamp == 1
        assert result.failed_emails == 0
        assert zoho.content_requests == [("Leads", "zoho-lead-1", "recent-message", "zoho-owner-1")]

        email = db.scalar(select(HubLeadEmail))
        assert email is not None
        assert "Aktuelle Anfrage" not in email.encrypted_payload_json
        assert "Vielen Dank" not in email.encrypted_payload_json
        views = HubLeadEmailService(db=db, cipher=cipher).list_email_views(lead_id=lead.id)
        assert len(views) == 1
        assert views[0].subject == "Aktuelle Anfrage"
        assert views[0].sender == "Erika Beispiel <erika@beispiel.de>"
        assert views[0].preview_html is not None
        assert "Vielen Dank für Ihre Anfrage." in views[0].preview_html

        repeat = importer.import_recent_emails(now=now)
        assert repeat.imported_emails == 0
        assert repeat.retained_emails == 1
        assert len(zoho.content_requests) == 1


def test_import_commits_small_batches_so_a_later_run_can_resume():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        lead = HubLead(zoho_id="zoho-lead-1", encrypted_profile_json=cipher.encrypt('{"fields": {}}'))
        db.add(lead)
        db.commit()

        importer = ZohoLeadEmailImportService(
            db=db,
            cipher=cipher,
            zoho_service=BatchFakeZohoLeadEmailService(),  # type: ignore[arg-type]
        )
        importer._COMMIT_BATCH_SIZE = 1
        result = importer.import_recent_emails(now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC))

        assert result.imported_emails == 2
        assert db.scalars(select(HubLeadEmail)).all()
