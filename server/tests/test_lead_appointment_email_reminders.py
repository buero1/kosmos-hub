import json
from datetime import UTC, datetime

from mailbox_fixture_helpers import mailbox_account
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_lead import HubLead
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_scheduled_email import HubScheduledEmail
from app.models.hub_user import HubUser
from app.models.zoho_email_template import ZohoEmailTemplate
from app.services.hub_mailbox_transport import (
    HubMailboxTransportDelivery,
    HubMailboxTransportService,
)
from app.services.lead_appointment_email_reminders import (
    LeadAppointmentEmailReminderService,
)
from app.services.scheduled_emails import ScheduledEmailService


def _cipher() -> SecretCipher:
    return SecretCipher("r" * 32)


def _profile(cipher: SecretCipher, **overrides: object) -> str:
    fields: dict[str, object] = {
        "first_name": "Lena",
        "last_name": "Leitner",
        "company": "Leitner Design",
        "email": "lena@example.test",
        "appointment_at": "2026-10-02T15:30",
        "appointment_reminder": True,
    }
    fields.update(overrides)
    return cipher.encrypt(
        json.dumps(
            {"schema_version": 1, "source": "hub", "fields": fields, "subforms": {}}
        )
    )


def _template(
    cipher: SecretCipher, *, template_id: str = "hub-template-reminder"
) -> ZohoEmailTemplate:
    return ZohoEmailTemplate(
        zoho_template_id=template_id,
        module="Leads",
        encrypted_payload_json=cipher.encrypt(
            json.dumps(
                {
                    "name": "Terminerinnerung",
                    "folder_name": "Leads Hub",
                    "subject": "Terminerinnerung ${Lead.AppointmentAt}",
                    "content": "<p>${Lead.Greeting}</p><p>Ihr Termin: ${Lead.AppointmentAt}</p>",
                    "hub_context_module": "leads",
                    "hub_content_reviewed_at": "2026-09-30T08:00:00+00:00",
                }
            )
        ),
        zoho_synced_at=datetime(2026, 9, 30, 8, 0, tzinfo=UTC),
        is_active=True,
    )


def _setup(db: Session, cipher: SecretCipher) -> tuple[HubUser, HubLead]:
    user = HubUser(
        username="hub-admin", password_hash="x", role="admin", is_active=True
    )
    db.add(user)
    db.flush()
    account = mailbox_account(db, cipher)
    user.default_sender_account_id = account.id
    lead = HubLead(encrypted_profile_json=_profile(cipher))
    db.add_all([lead, _template(cipher)])
    db.commit()
    return user, lead


def _jobs(db: Session) -> list[HubScheduledEmail]:
    return list(db.scalars(select(HubScheduledEmail).order_by(HubScheduledEmail.id)))


def test_reconcile_schedules_rendered_reminder_once_at_previous_day_11_berlin(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    now = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        ScheduledEmailService,
        "_utc_now",
        staticmethod(lambda: now.replace(tzinfo=None)),
    )

    with Session(engine) as db:
        _, lead = _setup(db, cipher)
        service = LeadAppointmentEmailReminderService(db=db, cipher=cipher)

        first = service.reconcile(now=now)
        db.commit()
        second = service.reconcile(now=now)
        db.commit()

        assert (first.scheduled, first.updated, first.cancelled, first.blocked) == (
            1,
            0,
            0,
            0,
        )
        assert not second.changed and second.blocked == 0
        jobs = _jobs(db)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.lead_id == lead.id
        assert job.scheduled_at == datetime(2026, 10, 1, 9, 0, tzinfo=UTC).replace(
            tzinfo=None
        )
        assert (
            job.automation_key
            == f"lead-appointment-reminder:{lead.id}:20261002T133000Z"
        )
        payload = ScheduledEmailService(db=db, cipher=cipher).payload(job)
        assert payload["sender_email"] == "info@kosmos-medien.de"
        assert payload["recipient_email"] == "lena@example.test"
        assert payload["template_id"] == "hub-template-reminder"
        assert "${" not in payload["subject"]
        assert "${" not in payload["content"]
        assert "Lena" in payload["content"]


def test_reconcile_replaces_changed_appointment_and_respects_manual_cancellation(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    now = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        ScheduledEmailService,
        "_utc_now",
        staticmethod(lambda: now.replace(tzinfo=None)),
    )

    with Session(engine) as db:
        _, lead = _setup(db, cipher)
        reminder = LeadAppointmentEmailReminderService(db=db, cipher=cipher)
        reminder.reconcile(now=now)
        db.commit()
        original = _jobs(db)[0]

        lead.encrypted_profile_json = _profile(
            cipher, appointment_at="2026-10-03T09:00"
        )
        moved = reminder.reconcile(now=now)
        db.commit()
        jobs = _jobs(db)
        assert moved.scheduled == 1 and moved.cancelled == 1
        assert jobs[0].id == original.id and jobs[0].status == "cancelled"
        assert jobs[1].status == "scheduled"
        assert jobs[1].scheduled_at == datetime(2026, 10, 2, 9, 0, tzinfo=UTC).replace(
            tzinfo=None
        )

        ScheduledEmailService(db=db, cipher=cipher).cancel(
            scheduled_email_id=jobs[1].id
        )
        db.commit()
        repeated = reminder.reconcile(now=now)
        db.commit()
        assert not repeated.changed
        assert len(_jobs(db)) == 2
        assert _jobs(db)[1].status == "cancelled"


def test_missing_exact_template_preserves_an_existing_scheduled_reminder(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    now = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        ScheduledEmailService,
        "_utc_now",
        staticmethod(lambda: now.replace(tzinfo=None)),
    )

    with Session(engine) as db:
        _setup(db, cipher)
        reminder = LeadAppointmentEmailReminderService(db=db, cipher=cipher)
        reminder.reconcile(now=now)
        db.commit()
        db.add(_template(cipher, template_id="hub-template-reminder-duplicate"))
        db.commit()

        result = reminder.reconcile(now=now)
        db.commit()

        assert result.blocked == 1
        assert not result.changed
        assert len(_jobs(db)) == 1
        assert _jobs(db)[0].status == "scheduled"


def test_reminder_flag_no_cancels_the_pending_email(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    now = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        ScheduledEmailService,
        "_utc_now",
        staticmethod(lambda: now.replace(tzinfo=None)),
    )

    with Session(engine) as db:
        _, lead = _setup(db, cipher)
        reminder = LeadAppointmentEmailReminderService(db=db, cipher=cipher)
        reminder.reconcile(now=now)
        db.commit()

        lead.encrypted_profile_json = _profile(cipher, appointment_reminder=False)
        result = reminder.reconcile(now=now)
        db.commit()

        assert result.cancelled == 1
        assert _jobs(db)[0].status == "cancelled"


def test_elapsed_planning_time_is_sent_once_and_linked_to_lead(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = _cipher()
    now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    clock = {"now": now.replace(tzinfo=None)}
    monkeypatch.setattr(
        ScheduledEmailService, "_utc_now", staticmethod(lambda: clock["now"])
    )
    deliveries: list[dict[str, object]] = []

    def fake_send(self, **kwargs):
        deliveries.append(kwargs)
        return HubMailboxTransportDelivery(
            message_id=kwargs["message_id"], sent_at=clock["now"].replace(tzinfo=UTC)
        )

    monkeypatch.setattr(HubMailboxTransportService, "send", fake_send)

    with Session(engine) as db:
        _, lead = _setup(db, cipher)
        reminder = LeadAppointmentEmailReminderService(db=db, cipher=cipher)
        result = reminder.reconcile(now=now)
        db.commit()
        job = _jobs(db)[0]
        assert result.scheduled == 1
        assert job.scheduled_at == clock["now"]

        scheduler = ScheduledEmailService(db=db, cipher=cipher)
        assert scheduler.process_due().sent == 1
        assert scheduler.process_due().sent == 0
        db.refresh(job)
        sent = db.get(HubMailboxEmail, job.mailbox_email_id)
        assert sent is not None
        payload = json.loads(cipher.decrypt(sent.encrypted_payload_json))
        assert payload["recipient_lead_id"] == lead.id
        assert job.status == "sent"
        assert len(deliveries) == 1
