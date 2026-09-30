"""Plan one durable customer email for eligible Lead consultation appointments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_lead import HubLead
from app.models.hub_scheduled_email import HubScheduledEmail
from app.models.hub_user import HubUser
from app.services.audit import write_audit_log
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_email_composition import render_template
from app.services.hub_mailbox_permissions import MailboxPermissions
from app.services.hub_operations import HubOperationService
from app.services.hub_workflows import (
    LEAD_APPOINTMENT_REMINDER_WORKFLOW_KEY,
    HubWorkflowService,
)
from app.services.scheduled_emails import ScheduledEmailService

_BERLIN = ZoneInfo("Europe/Berlin")
_AUTOMATION_PREFIX = "lead-appointment-reminder:"
_TEMPLATE_NAME = "Terminerinnerung"
_TEMPLATE_FOLDER = "Leads Hub"
_ACTIVE_STATUSES = {"scheduled", "retrying", "failed"}


@dataclass(frozen=True)
class LeadAppointmentEmailReminderResult:
    scheduled: int = 0
    updated: int = 0
    cancelled: int = 0
    blocked: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.scheduled or self.updated or self.cancelled)


class LeadAppointmentEmailReminderService:
    def __init__(
        self, *, db: Session, cipher: SecretCipher, public_base_url: str = ""
    ) -> None:
        self.db = db
        self.cipher = cipher
        self.public_base_url = public_base_url.rstrip("/")

    def reconcile(
        self, *, now: datetime | None = None
    ) -> LeadAppointmentEmailReminderResult:
        current = self._aware_utc(now or datetime.now(UTC))
        workflow_service = HubWorkflowService(db=self.db)
        workflow_service.ensure_default_workflows()
        workflow = next(
            item
            for item in workflow_service.list_workflows()
            if item.workflow_key == LEAD_APPOINTMENT_REMINDER_WORKFLOW_KEY
        )
        jobs = tuple(
            self.db.scalars(
                select(HubScheduledEmail)
                .where(HubScheduledEmail.automation_key.like(f"{_AUTOMATION_PREFIX}%"))
                .order_by(HubScheduledEmail.id)
            )
        )
        by_lead: dict[int, list[HubScheduledEmail]] = {}
        for job in jobs:
            if job.lead_id is not None:
                by_lead.setdefault(job.lead_id, []).append(job)
        cancelled = sum(
            self._cancel(job, "lead-deleted") for job in jobs if job.lead_id is None
        )
        if not workflow.is_enabled:
            cancelled += sum(self._cancel(job, "workflow-disabled") for job in jobs)
            return LeadAppointmentEmailReminderResult(cancelled=cancelled)

        runtime = self._runtime()
        if runtime is None:
            # A temporary template or mailbox configuration issue must not destroy
            # reminders that were already rendered and scheduled successfully.
            return LeadAppointmentEmailReminderResult(cancelled=cancelled, blocked=1)
        actor, sender_email, template_id = runtime
        scheduler = ScheduledEmailService(
            db=self.db,
            cipher=self.cipher,
            public_base_url=self.public_base_url,
        )
        scheduled = updated = blocked = 0
        for lead in self.db.scalars(select(HubLead).order_by(HubLead.id)):
            existing = by_lead.get(lead.id, [])
            values = self._lead_values(lead)
            eligible = self._eligible(values, current)
            if eligible is None:
                cancelled += sum(
                    self._cancel(job, "lead-not-eligible") for job in existing
                )
                continue
            appointment, recipient_email, recipient_name = eligible
            automation_key = self._automation_key(lead.id, appointment)
            cancelled += sum(
                self._cancel(job, "appointment-changed")
                for job in existing
                if job.automation_key != automation_key
            )
            current_job = next(
                (job for job in existing if job.automation_key == automation_key), None
            )
            if current_job is not None and current_job.status in {"sent", "sending"}:
                continue
            if current_job is not None and self._manual_cancelled(current_job):
                continue
            try:
                rendered = render_template(
                    HubOperationService(
                        db=self.db, cipher=self.cipher, actor=actor.username
                    ),
                    {
                        "template_id": template_id,
                        "lead_id": str(lead.id),
                        "context_module": "leads",
                        "context_record_id": str(lead.id),
                    },
                )
                if rendered.unresolved_placeholders:
                    raise ValueError(
                        "Die Terminerinnerungs-Vorlage enthält nicht aufgelöste Platzhalter."
                    )
                intended_at = self._send_at(appointment)
                fingerprint = self._fingerprint(
                    sender_email,
                    recipient_email,
                    rendered.subject,
                    rendered.content,
                    template_id,
                    intended_at,
                )
                if current_job is not None and self._unchanged(
                    current_job, fingerprint
                ):
                    continue
                job = scheduler.schedule(
                    actor=actor.username,
                    scheduled_at=intended_at,
                    sender_email=sender_email,
                    recipient_email=recipient_email,
                    recipient_name=recipient_name,
                    subject=rendered.subject,
                    content=rendered.content,
                    cc_emails="",
                    lead_id=lead.id,
                    template_id=template_id,
                    automation_key=automation_key,
                )
                payload = scheduler.payload(job)
                payload["automation_fingerprint"] = fingerprint
                payload["automation_manual_cancelled"] = False
                payload["automation_intended_at"] = intended_at.isoformat()
                job.encrypted_payload_json = self.cipher.encrypt(
                    json.dumps(payload, ensure_ascii=False)
                )
                action = "schedule" if current_job is None else "update"
                write_audit_log(
                    self.db,
                    site=None,
                    actor=actor.username,
                    source="hub-workflow",
                    action=f"{action}-lead-appointment-reminder",
                    result="ok",
                    detail=f"Lead {lead.id}: automated appointment reminder email {job.id} prepared.",
                )
                if current_job is None:
                    scheduled += 1
                else:
                    updated += 1
            except (ValueError, InvalidToken):
                blocked += 1
                if current_job is not None:
                    current_job.status = "failed"
                    current_job.last_error = "Die automatische Terminerinnerung konnte nicht vorbereitet werden."
        return LeadAppointmentEmailReminderResult(
            scheduled=scheduled,
            updated=updated,
            cancelled=cancelled,
            blocked=blocked,
        )

    def _runtime(self) -> tuple[HubUser, str, str] | None:
        actor = self.db.scalar(
            select(HubUser)
            .where(HubUser.role == "admin", HubUser.is_active.is_(True))
            .order_by(HubUser.id)
            .limit(1)
        )
        if actor is None:
            return None
        sender_email = MailboxPermissions(db=self.db, user=actor).default_sender(
            for_send=True
        )
        if not sender_email:
            return None
        communications = CustomerCommunicationService(
            db=self.db,
            cipher=self.cipher,
            actor=actor.username,
            public_base_url=self.public_base_url,
        )
        templates = tuple(
            item
            for item in communications.list_email_templates()
            if item.name.casefold() == _TEMPLATE_NAME.casefold()
            and item.category.casefold() == _TEMPLATE_FOLDER.casefold()
            and item.context_module == "leads"
        )
        if len(templates) != 1:
            return None
        return actor, sender_email, templates[0].id

    def _lead_values(self, lead: HubLead) -> dict[str, object]:
        try:
            profile = json.loads(self.cipher.decrypt(lead.encrypted_profile_json))
        except (InvalidToken, ValueError, TypeError):
            return {}
        values = profile.get("fields") if isinstance(profile, dict) else None
        return values if isinstance(values, dict) else {}

    @classmethod
    def _eligible(
        cls,
        values: dict[str, object],
        current: datetime,
    ) -> tuple[datetime, str, str] | None:
        if values.get("appointment_reminder") is not True:
            return None
        raw_appointment = str(values.get("appointment_at") or "").strip()
        recipient_email = str(values.get("email") or "").strip()
        if not raw_appointment or not recipient_email:
            return None
        try:
            appointment = datetime.fromisoformat(raw_appointment)
        except ValueError:
            return None
        appointment = (
            appointment.replace(tzinfo=_BERLIN)
            if appointment.tzinfo is None
            else appointment.astimezone(_BERLIN)
        )
        if appointment.astimezone(UTC) <= current:
            return None
        recipient_name = (
            " ".join(
                str(values.get(key) or "").strip()
                for key in ("first_name", "last_name")
            ).strip()
            or str(values.get("company") or "").strip()
            or recipient_email
        )
        return appointment, recipient_email, recipient_name

    @staticmethod
    def _send_at(appointment: datetime) -> datetime:
        local = datetime.combine(
            appointment.date() - timedelta(days=1), time(11), tzinfo=_BERLIN
        )
        return local.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _automation_key(lead_id: int, appointment: datetime) -> str:
        utc_value = appointment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{_AUTOMATION_PREFIX}{lead_id}:{utc_value}"

    @staticmethod
    def _fingerprint(*values: object) -> str:
        normalized = tuple(
            value.isoformat() if isinstance(value, datetime) else value
            for value in values
        )
        encoded = json.dumps(
            normalized, ensure_ascii=False, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _unchanged(self, job: HubScheduledEmail, fingerprint: str) -> bool:
        payload = ScheduledEmailService(db=self.db, cipher=self.cipher).payload(job)
        return (
            job.status in _ACTIVE_STATUSES
            and payload.get("automation_fingerprint") == fingerprint
        )

    def _manual_cancelled(self, job: HubScheduledEmail) -> bool:
        payload = ScheduledEmailService(db=self.db, cipher=self.cipher).payload(job)
        return (
            job.status == "cancelled"
            and payload.get("automation_manual_cancelled") is True
        )

    def _cancel(self, job: HubScheduledEmail, reason: str) -> int:
        if job.status not in _ACTIVE_STATUSES:
            return 0
        payload = ScheduledEmailService(db=self.db, cipher=self.cipher).payload(job)
        payload["automation_manual_cancelled"] = False
        payload["automation_cancel_reason"] = reason
        job.encrypted_payload_json = self.cipher.encrypt(
            json.dumps(payload, ensure_ascii=False)
        )
        job.status = "cancelled"
        job.locked_at = None
        job.last_error = None
        write_audit_log(
            self.db,
            site=None,
            actor=job.creator_username,
            source="hub-workflow",
            action="cancel-lead-appointment-reminder",
            result="ok",
            detail=f"Lead {job.lead_id}: automated appointment reminder email {job.id} cancelled.",
        )
        return 1

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
