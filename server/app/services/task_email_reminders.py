"""Durable server-side delivery for task email reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from html import escape
from secrets import token_hex
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_activity import CustomerTaskActivity
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_user import HubUser
from app.services.hub_mailbox import HubMailboxService, TASK_EMAIL_REMINDER_SOURCE
from app.services.hub_mailbox_transport import DEFAULT_HUB_MAILBOX_SENDER_EMAIL


_PENDING_STATUSES = ("scheduled", "retrying")
_RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60))
_STALE_SENDING_AFTER = timedelta(minutes=15)
_BERLIN = ZoneInfo("Europe/Berlin")


class TaskEmailReminderError(ValueError):
    """Raised when a task cannot receive the requested email reminder."""


@dataclass(frozen=True)
class TaskEmailReminderProcessResult:
    sent: int = 0
    retried: int = 0
    failed: int = 0
    cancelled: int = 0


class TaskEmailReminderService:
    """Keep a task's reminder job in sync and deliver only due jobs."""

    def __init__(self, *, db: Session, cipher: SecretCipher | None = None, public_base_url: str = "") -> None:
        self.db = db
        self.cipher = cipher
        self.public_base_url = public_base_url.rstrip("/")

    def sync_task(self, *, task: CustomerTaskActivity) -> CustomerTaskEmailReminder | None:
        jobs = self.sync_activity(activity=task, kind="task")
        return jobs[0] if jobs else None

    @staticmethod
    def specifications(activity, kind):
        if activity.status != "planned":
            return {}
        if kind == "task":
            return {"primary": activity.reminder_minutes_before} if TaskEmailReminderService._requires_email_reminder(activity) else {}
        if activity.starts_at is None:
            return {}
        reminders = list(activity.reminders)
        if not reminders and kind == "call" and activity.reminder_channel == "email":
            return {"email:" + str(activity.reminder_minutes_before or 0): activity.reminder_minutes_before or 0}
        return {"email:" + str(row.minutes_before): row.minutes_before for row in reminders if row.channel == "email"}

    def sync_activity(self, *, activity, kind):
        jobs = list(self.db.scalars(select(CustomerTaskEmailReminder).where(
            CustomerTaskEmailReminder.activity_kind == kind,
            CustomerTaskEmailReminder.activity_id == activity.id)))
        specifications = self.specifications(activity, kind)
        owner = self.db.get(HubUser, activity.assignee_user_id) if activity.assignee_user_id else None
        if not owner or not owner.is_active:
            specifications = {}
        email = self._reminder_email_for(owner.username) if specifications else ""
        start = activity.due_at if kind == "task" else activity.starts_at
        customer = self.db.get(Customer, activity.customer_id) if activity.customer_id else None
        by_key = {job.reminder_key: job for job in jobs}
        for job in jobs:
            if job.reminder_key not in specifications and job.status in {*_PENDING_STATUSES, "sending"}:
                job.status, job.locked_at, job.last_error = "cancelled", None, None
        for key, minutes in specifications.items():
            scheduled_at = start - timedelta(minutes=minutes)
            job = by_key.get(key)
            if job is not None and job.status == "sent" and not self._sent_reminder_was_rescheduled(
                    reminder=job, scheduled_at=scheduled_at, minutes_before=minutes):
                continue
            if job is None:
                job = CustomerTaskEmailReminder(task_id=activity.id if kind == "task" else None,
                    activity_kind=kind, activity_id=activity.id, reminder_key=key)
                self.db.add(job)
                jobs.append(job)
            was_sent = job.status == "sent"
            job.customer_id = activity.customer_id
            job.creator_username = activity.created_by_username or ""
            job.task_name, job.task_description = activity.name, activity.description
            job.customer_name = customer.name if customer else ""
            job.recipient_user_id, job.recipient_email = owner.id, email
            job.sender_email = DEFAULT_HUB_MAILBOX_SENDER_EMAIL
            job.minutes_before, job.scheduled_at, job.next_attempt_at = minutes, scheduled_at, scheduled_at
            job.status, job.attempt_count, job.last_error, job.locked_at = "scheduled", 0, None, None
            if was_sent or not job.message_id:
                job.sent_at, job.mailbox_email_id, job.message_id = None, None, self._new_message_id()
        self.db.flush()
        return jobs

    @staticmethod
    def _sent_reminder_was_rescheduled(*, reminder, scheduled_at, minutes_before):
        return reminder.scheduled_at != scheduled_at or reminder.minutes_before != minutes_before

    def assert_not_sending(self, *, activity, kind):
        sending = self.db.scalar(select(CustomerTaskEmailReminder.id).where(
            CustomerTaskEmailReminder.activity_kind == kind, CustomerTaskEmailReminder.activity_id == activity.id,
            CustomerTaskEmailReminder.status == "sending"))
        if sending is not None:
            raise TaskEmailReminderError("Eine Erinnerung wird gerade versendet. Bitte kurz warten und die Aenderung erneut speichern.")

    def cancel_activity(self, *, activity, kind):
        self.assert_not_sending(activity=activity, kind=kind)
        for job in self.db.scalars(select(CustomerTaskEmailReminder).where(
                CustomerTaskEmailReminder.activity_kind == kind, CustomerTaskEmailReminder.activity_id == activity.id)):
            if job.status in _PENDING_STATUSES:
                job.status, job.locked_at, job.last_error = "cancelled", None, None
            job.task_id = None
            job.activity_id = None
        self.db.flush()

    def cancel_for_deleted_task(self, *, task):
        self.cancel_activity(activity=task, kind="task")

    def next_due_at(self) -> datetime | None:
        return self.db.scalar(
            select(func.min(CustomerTaskEmailReminder.next_attempt_at)).where(
                CustomerTaskEmailReminder.status.in_(_PENDING_STATUSES)
            )
        )

    def process_due_reminders(self, *, limit: int = 25) -> TaskEmailReminderProcessResult:
        if self.cipher is None:
            raise RuntimeError("Für den Versand von Aufgaben-Erinnerungen fehlt die Serververschlüsselung.")
        now = self._utc_now()
        recovered_retries, recovered_failures = self._recover_stalled_deliveries(now=now)
        due_ids = list(
            self.db.scalars(
                select(CustomerTaskEmailReminder.id)
                .where(CustomerTaskEmailReminder.status.in_(_PENDING_STATUSES))
                .where(CustomerTaskEmailReminder.next_attempt_at <= now)
                .order_by(CustomerTaskEmailReminder.next_attempt_at.asc(), CustomerTaskEmailReminder.id.asc())
                .limit(limit)
            )
        )
        sent = cancelled = 0
        retried = recovered_retries
        failed = recovered_failures
        for reminder_id in due_ids:
            outcome = self._deliver_reminder(reminder_id=reminder_id)
            sent += outcome.sent
            retried += outcome.retried
            failed += outcome.failed
            cancelled += outcome.cancelled
        return TaskEmailReminderProcessResult(sent=sent, retried=retried, failed=failed, cancelled=cancelled)

    def _deliver_reminder(self, *, reminder_id: int) -> TaskEmailReminderProcessResult:
        now = self._utc_now()
        reminder = self.db.get(CustomerTaskEmailReminder, reminder_id)
        if reminder is None or reminder.status not in _PENDING_STATUSES or reminder.next_attempt_at > now:
            return TaskEmailReminderProcessResult()
        from app.services.hub_activity_responsibility import ACTIVITY_MODELS, ActivityResponsibility
        model = ACTIVITY_MODELS.get(reminder.activity_kind)
        task = self.db.scalar(select(model).where(model.id == reminder.activity_id).with_for_update()) if model else None
        reminder = self.db.scalar(select(CustomerTaskEmailReminder).where(CustomerTaskEmailReminder.id == reminder_id)
            .with_for_update().execution_options(populate_existing=True))
        if reminder is None or reminder.status not in _PENDING_STATUSES or reminder.next_attempt_at > now:
            self.db.rollback()
            return TaskEmailReminderProcessResult()
        owner = self.db.get(HubUser, task.assignee_user_id) if task and task.assignee_user_id else None
        specs = self.specifications(task, reminder.activity_kind) if task else {}
        valid = bool(owner and owner.is_active and owner.id == reminder.recipient_user_id
                     and ActivityResponsibility(self.db, owner).visible(task)
                     and specs.get(reminder.reminder_key) == reminder.minutes_before
                     and reminder.reminder_key in specs)
        if valid:
            start = task.due_at if reminder.activity_kind == "task" else task.starts_at
            valid = start - timedelta(minutes=reminder.minutes_before) == reminder.scheduled_at
        if valid:
            try:
                reminder.recipient_email = self._reminder_email_for(owner.username)
            except TaskEmailReminderError:
                valid = False
        if not valid:
            reminder.status = "cancelled"
            reminder.locked_at = None
            self.db.commit()
            return TaskEmailReminderProcessResult(cancelled=1)

        reminder.status = "sending"
        reminder.locked_at = now
        self.db.commit()
        try:
            sent_email = HubMailboxService(
                db=self.db,
                cipher=self.cipher,
                public_base_url=self.public_base_url,
            ).send_direct_email(
                sender_email=reminder.sender_email,
                recipient_email=reminder.recipient_email,
                subject=f"Hub-Erinnerung: {reminder.task_name}",
                content=self._email_html(reminder),
                cc_emails="",
                source=TASK_EMAIL_REMINDER_SOURCE,
                message_id=reminder.message_id,
            )
        except Exception as exc:
            self.db.rollback()
            return self._record_delivery_failure(reminder_id=reminder_id, error=str(exc))

        reminder = self.db.get(CustomerTaskEmailReminder, reminder_id)
        if reminder is None:
            self.db.commit()
            return TaskEmailReminderProcessResult()
        reminder.status = "sent"
        reminder.sent_at = self._utc_now()
        reminder.locked_at = None
        reminder.last_error = None
        reminder.mailbox_email_id = sent_email.id
        self.db.commit()
        return TaskEmailReminderProcessResult(sent=1)

    def _record_delivery_failure(self, *, reminder_id: int, error: str) -> TaskEmailReminderProcessResult:
        reminder = self.db.get(CustomerTaskEmailReminder, reminder_id)
        if reminder is None:
            return TaskEmailReminderProcessResult()
        reminder.attempt_count += 1
        reminder.locked_at = None
        reminder.last_error = (error.strip() or "Der Versand konnte nicht abgeschlossen werden.")[:2_000]
        if reminder.attempt_count > len(_RETRY_DELAYS):
            reminder.status = "failed"
            self.db.commit()
            return TaskEmailReminderProcessResult(failed=1)
        reminder.status = "retrying"
        reminder.next_attempt_at = self._utc_now() + _RETRY_DELAYS[reminder.attempt_count - 1]
        self.db.commit()
        return TaskEmailReminderProcessResult(retried=1)

    def _recover_stalled_deliveries(self, *, now: datetime) -> tuple[int, int]:
        stale = list(
            self.db.scalars(
                select(CustomerTaskEmailReminder)
                .where(CustomerTaskEmailReminder.status == "sending")
                .where(CustomerTaskEmailReminder.locked_at.is_not(None))
                .where(CustomerTaskEmailReminder.locked_at <= now - _STALE_SENDING_AFTER)
            )
        )
        retried = failed = 0
        for reminder in stale:
            reminder.attempt_count += 1
            reminder.locked_at = None
            reminder.last_error = "Der Hub-Dienst wurde während des Versands neu gestartet."
            if reminder.attempt_count > len(_RETRY_DELAYS):
                reminder.status = "failed"
                failed += 1
            else:
                reminder.status = "retrying"
                reminder.next_attempt_at = now
                retried += 1
        if retried or failed:
            self.db.commit()
        return retried, failed

    def _reminder_email_for(self, username: str | None) -> str:
        owner = None if not username else self.db.scalar(select(HubUser).where(HubUser.username == username))
        email = "" if owner is None or owner.reminder_email is None else owner.reminder_email.strip()
        _name, parsed_email = parseaddr(email)
        if not parsed_email or "@" not in parsed_email or len(parsed_email) > 320:
            raise TaskEmailReminderError(
                "Für E-Mail-Erinnerungen zuerst unter Account → Benutzer eine Erinnerungsadresse beim Benutzer speichern."
            )
        return parsed_email.casefold()

    @staticmethod
    def _requires_email_reminder(task: CustomerTaskActivity) -> bool:
        return (
            task.status == "planned"
            and task.reminder_channel == "email"
            and task.reminder_minutes_before is not None
            and task.due_at is not None
        )

    def _email_html(self, reminder: CustomerTaskEmailReminder) -> str:
        due_at = reminder.scheduled_at + timedelta(minutes=reminder.minutes_before)
        due_text = due_at.replace(tzinfo=UTC).astimezone(_BERLIN).strftime("%d.%m.%Y um %H:%M Uhr")
        reminder_text = "zum Fälligkeitszeitpunkt" if reminder.minutes_before == 0 else (
            f"{reminder.minutes_before} Minuten vorher"
        )
        description = ""
        if reminder.task_description:
            description = (
                "<tr><td style=\"padding: 6px 0; color: #555; vertical-align: top;\"><strong>Beschreibung</strong></td>"
                f"<td style=\"padding: 6px 0;\">{escape(reminder.task_description).replace(chr(10), '<br>')}</td></tr>"
            )
        customer_url = self._customer_url(reminder)
        task_url = self._task_url(reminder)
        task_name = escape(reminder.task_name)
        if task_url:
            task_name = self._hub_link(url=task_url, label=task_name)
        customer_row = ""
        if reminder.customer_name:
            customer_name = escape(reminder.customer_name)
            if customer_url:
                customer_name = self._hub_link(url=customer_url, label=customer_name)
            customer_row = (
                "<tr><td style=\"padding: 6px 0; color: #555; vertical-align: top;\"><strong>Kunde</strong></td>"
                f"<td style=\"padding: 6px 0;\">{customer_name}</td></tr>"
            )
        return (
            "<p>Erinnerung an Ihre Aktivität:</p>"
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="border-collapse: collapse;">'
            "<tbody>"
            "<tr><td style=\"padding: 6px 0; color: #555; vertical-align: top; width: 150px;\"><strong>Aktivität</strong></td>"
            f"<td style=\"padding: 6px 0;\">{task_name}</td></tr>"
            f"{customer_row}"
            "<tr><td style=\"padding: 6px 0; color: #555; vertical-align: top;\"><strong>Fällig</strong></td>"
            f"<td style=\"padding: 6px 0;\">{due_text}</td></tr>"
            "<tr><td style=\"padding: 6px 0; color: #555; vertical-align: top;\"><strong>Erinnerung</strong></td>"
            f"<td style=\"padding: 6px 0;\">{reminder_text}</td></tr>"
            f"{description}</tbody></table>"
        )

    def _customer_url(self, reminder: CustomerTaskEmailReminder) -> str | None:
        if reminder.customer_id is None or not self.public_base_url:
            return None
        return f"{self.public_base_url}/customers/{reminder.customer_id}"

    def _task_url(self, reminder: CustomerTaskEmailReminder) -> str | None:
        if reminder.activity_kind != "task":
            return f"{self.public_base_url}/activities/{reminder.activity_kind}/{reminder.activity_id}" if self.public_base_url and reminder.activity_id else None
        customer_url = self._customer_url(reminder)
        if reminder.task_id is None or not self.public_base_url:
            return None
        return f"{customer_url}#task-{reminder.task_id}" if customer_url else (
            f"{self.public_base_url}/activities/task/{reminder.task_id}"
        )

    @staticmethod
    def _hub_link(*, url: str, label: str) -> str:
        return (
            f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer" '
            'style="color: #1266e8; text-decoration: underline;">'
            f"{label}</a>"
        )

    @staticmethod
    def _new_message_id() -> str:
        return f"<hub-task-reminder-{token_hex(20)}@kosmos-medien.de>"

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
