from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerMeetingActivity, CustomerTaskActivity
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.hub_user import HubUser


SNOOZE_MINUTES_OPTIONS = (1, 5, 10, 15, 30, 60, 120, 240, 480, 720, 1440, 2880, 4320, 5760, 10080, 20160)
_ACTIVITY_MODELS = {
    "call": CustomerCallActivity,
    "meeting": CustomerMeetingActivity,
    "task": CustomerTaskActivity,
}
_ACTIVITY_LABELS = {
    "call": "Anruf",
    "meeting": "Meeting",
    "task": "Aufgabe",
}


class DesktopReminderError(ValueError):
    """Raised when a desktop reminder action is invalid."""


@dataclass(frozen=True)
class DesktopReminderView:
    id: int
    customer_id: int | None
    activity_kind: str
    activity_label: str
    activity_name: str
    customer_name: str
    starts_at: datetime
    remind_at: datetime
    due_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "activity_kind": self.activity_kind,
            "activity_label": self.activity_label,
            "activity_name": self.activity_name,
            "customer_name": self.customer_name,
            "starts_at": self.starts_at.replace(tzinfo=UTC).isoformat(),
            "remind_at": self.remind_at.replace(tzinfo=UTC).isoformat(),
            "due_at": self.due_at.replace(tzinfo=UTC).isoformat(),
        }


class CustomerDesktopReminderService:
    """Materializes due Popup reminders and records desktop actions centrally."""

    def __init__(self, *, db: Session):
        self.db = db

    def list_due_reminders(self, *, user: HubUser) -> tuple[DesktopReminderView, ...]:
        now = self._utc_now()
        self._materialize_due_popup_reminders(user=user, now=now)
        notifications = self.db.scalars(
            select(CustomerActivityReminderNotification)
            .where(CustomerActivityReminderNotification.user_id == user.id)
            .where(CustomerActivityReminderNotification.completed_at.is_(None))
            .order_by(CustomerActivityReminderNotification.remind_at.asc(), CustomerActivityReminderNotification.id.asc())
        ).all()
        customer_names: dict[int, str] = {}
        views: list[DesktopReminderView] = []
        for notification in notifications:
            due_at = notification.snoozed_until or notification.remind_at
            if due_at > now:
                continue
            activity = self._activity_for_notification(notification)
            if activity is None or activity.status != "planned":
                notification.completed_at = now
                notification.snoozed_until = None
                continue
            if notification.customer_id is None:
                customer_name = ""
            else:
                customer_name = customer_names.get(notification.customer_id)
                if customer_name is None:
                    customer = self.db.get(Customer, notification.customer_id)
                    customer_name = customer.name if customer is not None else "Unbekannter Kunde"
                    customer_names[notification.customer_id] = customer_name
            starts_at = self._activity_starts_at(activity)
            if starts_at is None:
                notification.completed_at = now
                continue
            views.append(
                DesktopReminderView(
                    id=notification.id,
                    customer_id=notification.customer_id,
                    activity_kind=notification.activity_kind,
                    activity_label=_ACTIVITY_LABELS[notification.activity_kind],
                    activity_name=activity.name,
                    customer_name=customer_name,
                    starts_at=starts_at,
                    remind_at=notification.remind_at,
                    due_at=due_at,
                )
            )
        self.db.flush()
        return tuple(views)

    def snooze_reminders(self, *, user: HubUser, notification_ids: list[int], minutes: int) -> int:
        if minutes not in SNOOZE_MINUTES_OPTIONS:
            raise DesktopReminderError("Bitte eine gültige Dauer zum erneuten Erinnern wählen.")
        notifications = self._active_notifications_for_user(user=user, notification_ids=notification_ids)
        until = self._utc_now() + timedelta(minutes=minutes)
        for notification in notifications:
            notification.snoozed_until = until
        self.db.flush()
        return len(notifications)

    def snooze_reminders_before_start(
        self,
        *,
        user: HubUser,
        notification_ids: list[int],
        minutes_before: int,
    ) -> int:
        if minutes_before not in {0, 5}:
            raise DesktopReminderError("Bitte einen gültigen Zeitpunkt vor dem Start wählen.")
        notifications = self._active_notifications_for_user(user=user, notification_ids=notification_ids)
        now = self._utc_now()
        for notification in notifications:
            activity = self._activity_for_notification(notification)
            starts_at = self._activity_starts_at(activity) if activity is not None else None
            if starts_at is None:
                raise DesktopReminderError("Die Aktivität zu einer Erinnerung konnte nicht gefunden werden.")
            target = starts_at - timedelta(minutes=minutes_before)
            # A late click cannot restore a past appointment time; keep it visible shortly instead.
            notification.snoozed_until = target if target > now else now + timedelta(minutes=1)
        self.db.flush()
        return len(notifications)

    def complete_reminders(self, *, user: HubUser, notification_ids: list[int]) -> int:
        notifications = self._active_notifications_for_user(user=user, notification_ids=notification_ids)
        now = self._utc_now()
        completed_sources: set[tuple[str, int]] = set()
        for notification in notifications:
            source = (notification.activity_kind, notification.activity_id)
            if source in completed_sources:
                continue
            activity = self._activity_for_notification(notification)
            if activity is not None:
                activity.status = "completed"
            completed_sources.add(source)

        for activity_kind, activity_id in completed_sources:
            self.db.execute(
                update(CustomerActivityReminderNotification)
                .where(CustomerActivityReminderNotification.user_id == user.id)
                .where(CustomerActivityReminderNotification.activity_kind == activity_kind)
                .where(CustomerActivityReminderNotification.activity_id == activity_id)
                .where(CustomerActivityReminderNotification.completed_at.is_(None))
                .values(completed_at=now, snoozed_until=None)
            )
        self.db.flush()
        return len(notifications)

    def _materialize_due_popup_reminders(self, *, user: HubUser, now: datetime) -> None:
        calls = self.db.scalars(
            select(CustomerCallActivity)
            .options(selectinload(CustomerCallActivity.reminders))
            .where(CustomerCallActivity.created_by_username == user.username)
            .where(CustomerCallActivity.status == "planned")
        ).all()
        for call in calls:
            reminders = tuple(
                (str(reminder.sort_order), reminder.channel, reminder.minutes_before)
                for reminder in call.reminders
            )
            if not reminders and call.reminder_channel and call.reminder_minutes_before is not None:
                reminders = (("legacy", call.reminder_channel, call.reminder_minutes_before),)
            self._materialize_activity_reminders(
                user=user,
                now=now,
                activity_kind="call",
                activity_id=call.id,
                customer_id=call.customer_id,
                starts_at=call.starts_at,
                reminders=reminders,
            )

        meetings = self.db.scalars(
            select(CustomerMeetingActivity)
            .options(selectinload(CustomerMeetingActivity.reminders))
            .where(CustomerMeetingActivity.created_by_username == user.username)
            .where(CustomerMeetingActivity.status == "planned")
        ).all()
        for meeting in meetings:
            self._materialize_activity_reminders(
                user=user,
                now=now,
                activity_kind="meeting",
                activity_id=meeting.id,
                customer_id=meeting.customer_id,
                starts_at=meeting.starts_at,
                reminders=tuple(
                    (str(reminder.sort_order), reminder.channel, reminder.minutes_before)
                    for reminder in meeting.reminders
                ),
            )

        tasks = self.db.scalars(
            select(CustomerTaskActivity)
            .where(CustomerTaskActivity.created_by_username == user.username)
            .where(CustomerTaskActivity.status == "planned")
        ).all()
        for task in tasks:
            if task.reminder_channel is None or task.reminder_minutes_before is None:
                continue
            self._materialize_activity_reminders(
                user=user,
                now=now,
                activity_kind="task",
                activity_id=task.id,
                customer_id=task.customer_id,
                starts_at=task.due_at,
                reminders=(("primary", task.reminder_channel, task.reminder_minutes_before),),
            )

    def _materialize_activity_reminders(
        self,
        *,
        user: HubUser,
        now: datetime,
        activity_kind: str,
        activity_id: int,
        customer_id: int | None,
        starts_at: datetime | None,
        reminders: tuple[tuple[str, str, int], ...],
    ) -> None:
        if starts_at is None:
            return
        for reminder_key, channel, minutes_before in reminders:
            if channel != "popup":
                continue
            remind_at = starts_at - timedelta(minutes=minutes_before)
            if remind_at > now:
                continue
            notification = self.db.scalar(
                select(CustomerActivityReminderNotification).where(
                    CustomerActivityReminderNotification.user_id == user.id,
                    CustomerActivityReminderNotification.activity_kind == activity_kind,
                    CustomerActivityReminderNotification.activity_id == activity_id,
                    CustomerActivityReminderNotification.reminder_key == reminder_key,
                )
            )
            if notification is None:
                self.db.add(
                    CustomerActivityReminderNotification(
                        user_id=user.id,
                        customer_id=customer_id,
                        activity_kind=activity_kind,
                        activity_id=activity_id,
                        reminder_key=reminder_key,
                        remind_at=remind_at,
                    )
                )

    def _active_notifications_for_user(
        self,
        *,
        user: HubUser,
        notification_ids: list[int],
    ) -> list[CustomerActivityReminderNotification]:
        normalized_ids = tuple(dict.fromkeys(notification_ids))
        if not normalized_ids or len(normalized_ids) > 100 or any(identifier <= 0 for identifier in normalized_ids):
            raise DesktopReminderError("Bitte mindestens eine gültige Erinnerung auswählen.")
        notifications = list(
            self.db.scalars(
                select(CustomerActivityReminderNotification)
                .where(CustomerActivityReminderNotification.user_id == user.id)
                .where(CustomerActivityReminderNotification.id.in_(normalized_ids))
                .where(CustomerActivityReminderNotification.completed_at.is_(None))
            )
        )
        if len(notifications) != len(normalized_ids):
            raise DesktopReminderError("Eine oder mehrere Erinnerungen sind nicht mehr verfügbar.")
        return notifications

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    @staticmethod
    def _activity_starts_at(activity: CustomerCallActivity | CustomerMeetingActivity | CustomerTaskActivity) -> datetime | None:
        if isinstance(activity, CustomerTaskActivity):
            return activity.due_at
        return activity.starts_at

    def _activity_for_notification(
        self,
        notification: CustomerActivityReminderNotification,
    ) -> CustomerCallActivity | CustomerMeetingActivity | CustomerTaskActivity | None:
        model = _ACTIVITY_MODELS.get(notification.activity_kind)
        return None if model is None else self.db.get(model, notification.activity_id)
