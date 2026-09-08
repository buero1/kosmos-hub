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
        reminder = self.db.scalar(
            select(CustomerTaskEmailReminder).where(CustomerTaskEmailReminder.task_id == task.id)
        )
        if not self._requires_email_reminder(task):
            if reminder is not None and reminder.status in {*_PENDING_STATUSES, "sending"}:
                reminder.status = "cancelled"
                reminder.locked_at = None
                reminder.last_error = None
            self.db.flush()
            return reminder

        recipient_email = self._reminder_email_for(task.created_by_username)
        scheduled_at = task.due_at - timedelta(minutes=task.reminder_minutes_before or 0)
        customer = self.db.get(Customer, task.customer_id)
        customer_name = customer.name if customer is not None else ""
        if reminder is None:
            reminder = CustomerTaskEmailReminder(
                task_id=task.id,
                customer_id=task.customer_id,
                creator_username=task.created_by_username or "",
                task_name=task.name,
                task_description=task.description,
                customer_name=customer_name,
                recipient_email=recipient_email,
                sender_email=DEFAULT_HUB_MAILBOX_SENDER_EMAIL,
                minutes_before=task.reminder_minutes_before or 0,
                scheduled_at=scheduled_at,
                next_attempt_at=scheduled_at,
                message_id=self._new_message_id(),
            )
            self.db.add(reminder)
        elif reminder.status != "sent" or self._sent_reminder_was_rescheduled(
            reminder=reminder,
            scheduled_at=scheduled_at,
            minutes_before=task.reminder_minutes_before or 0,
        ):
            was_sent = reminder.status == "sent"
            reminder.customer_id = task.customer_id
            reminder.creator_username = task.created_by_username or ""
            reminder.task_name = task.name
            reminder.task_description = task.description
            reminder.customer_name = customer_name
            reminder.recipient_email = recipient_email
            reminder.sender_email = DEFAULT_HUB_MAILBOX_SENDER_EMAIL
            reminder.minutes_before = task.reminder_minutes_before or 0
            reminder.scheduled_at = scheduled_at
            reminder.next_attempt_at = scheduled_at
            reminder.status = "scheduled"
            reminder.attempt_count = 0
            reminder.last_error = None
            reminder.locked_at = None
            if was_sent:
                # A deliberate move of an already completed reminder is a new delivery, not a retry.
                reminder.sent_at = None
                reminder.mailbox_email_id = None
                reminder.message_id = self._new_message_id()
        self.db.flush()
        return reminder

    @staticmethod
    def _sent_reminder_was_rescheduled(
        *,
        reminder: CustomerTaskEmailReminder,
        scheduled_at: datetime,
        minutes_before: int,
    ) -> bool:
        """Only re-open a delivered reminder when its delivery time actually changed."""
        return reminder.scheduled_at != scheduled_at or reminder.minutes_before != minutes_before

    def cancel_for_deleted_task(self, *, task: CustomerTaskActivity) -> None:
        reminder = self.db.scalar(
            select(CustomerTaskEmailReminder).where(CustomerTaskEmailReminder.task_id == task.id)
        )
        if reminder is None:
            return
        if reminder.status in {*_PENDING_STATUSES, "sending"}:
            reminder.status = "cancelled"
            reminder.locked_at = None
            reminder.last_error = None
        reminder.task_id = None
        self.db.flush()

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
        task = self.db.get(CustomerTaskActivity, reminder.task_id) if reminder.task_id is not None else None
        if task is None or not self._requires_email_reminder(task):
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
                "Für E-Mail-Erinnerungen zuerst unter Account eine persönliche Erinnerungsadresse speichern."
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
            "<p>Diese Aufgabe ist fällig:</p>"
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="border-collapse: collapse;">'
            "<tbody>"
            "<tr><td style=\"padding: 6px 0; color: #555; vertical-align: top; width: 150px;\"><strong>Aufgabe</strong></td>"
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
        customer_url = self._customer_url(reminder)
        if reminder.task_id is None or customer_url is None:
            return None
        return f"{customer_url}#task-{reminder.task_id}"

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
