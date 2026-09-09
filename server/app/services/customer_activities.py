from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerCallReminder, CustomerMeetingActivity, CustomerMeetingReminder, CustomerTaskActivity
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.services.task_email_reminders import TaskEmailReminderError, TaskEmailReminderService


CALL_STATUS_OPTIONS = (
    ("planned", "Geplant"),
    ("completed", "Abgeschlossen"),
    ("cancelled", "Abgesagt"),
)
CALL_DIRECTION_OPTIONS = (
    ("outbound", "Ausgehend"),
    ("inbound", "Eingehend"),
)
CALL_DURATION_OPTIONS = (5, 10, 15, 30, 45, 60, 90, 120)
CALL_TIME_OPTIONS = tuple(
    f"{hour:02d}:{minute:02d}"
    for hour in range(24)
    for minute in (0, 30)
)
CALL_REMINDER_OPTIONS = (0, 5, 10, 15, 30, 60)
CALL_REMINDER_CHANNEL_OPTIONS = (
    ("popup", "Popup"),
    ("email", "E-Mail"),
    ("none", "Keine Erinnerung"),
)


def suggested_call_start(now: datetime) -> datetime:
    """Choose the next half-hour slot, skipping slots with less than ten minutes left."""
    if now.minute < 30:
        candidate = now.replace(minute=30, second=0, microsecond=0)
    else:
        candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    if candidate - now < timedelta(minutes=10):
        candidate += timedelta(minutes=30)
    return candidate


class CustomerActivityError(ValueError):
    """Raised when an activity cannot be created from submitted form data."""


@dataclass(frozen=True)
class CustomerCallActivityView:
    id: int
    name: str
    status: str
    direction: str
    starts_at: datetime
    start_date: str
    start_time: str
    ends_at: datetime
    duration_minutes: int
    reminders: tuple["CustomerCallReminderView", ...]
    description: str | None


@dataclass(frozen=True)
class CustomerCallReminderView:
    channel: str
    minutes_before: int


@dataclass(frozen=True)
class CustomerCallValues:
    name: str
    status: str
    direction: str
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    reminders: tuple[CustomerCallReminderView, ...]
    description: str | None


@dataclass(frozen=True)
class CustomerTaskActivityView:
    id: int
    name: str
    status: str
    due_at: datetime
    due_date: str
    due_time: str
    reminder_channel: str | None
    reminder_minutes_before: int | None
    email_reminder_status: str | None
    email_reminder_last_error: str | None
    description: str | None


@dataclass(frozen=True)
class CustomerMeetingActivityView:
    id: int
    name: str
    status: str
    starts_at: datetime
    start_date: str
    start_time: str
    end_date: str
    end_time: str
    duration_minutes: int
    reminders: tuple["CustomerMeetingReminderView", ...]
    description: str | None


@dataclass(frozen=True)
class CustomerMeetingReminderView:
    channel: str
    minutes_before: int


@dataclass(frozen=True)
class CustomerCalendarActivityView:
    """A call or meeting positioned in the Berlin-time weekly calendar."""

    id: int
    kind: str
    name: str
    status: str
    customer_id: int | None
    customer_name: str
    direction: str | None
    start_date: str
    start_time: str
    end_time: str
    duration_minutes: int
    reminder_channels: tuple[str, ...]
    reminder_minutes_before: tuple[int, ...]
    description: str | None
    start_slot: float
    slot_span: float
    column_index: int = 0
    column_count: int = 1


class CustomerActivityService:
    def __init__(self, *, db: Session):
        self.db = db

    def list_calls(self, *, customer_id: int) -> tuple[CustomerCallActivityView, ...]:
        calls = self.db.scalars(
            select(CustomerCallActivity)
            .options(selectinload(CustomerCallActivity.reminders))
            .where(CustomerCallActivity.customer_id == customer_id)
            .where(CustomerCallActivity.status == "planned")
            .order_by(CustomerCallActivity.starts_at.asc(), CustomerCallActivity.id.asc())
        ).all()
        return tuple(
            self._call_view(call)
            for call in calls
        )

    def list_tasks(self, *, customer_id: int) -> tuple[CustomerTaskActivityView, ...]:
        tasks = self.db.scalars(
            select(CustomerTaskActivity)
            .where(CustomerTaskActivity.customer_id == customer_id)
            .where(CustomerTaskActivity.status == "planned")
            .order_by(CustomerTaskActivity.due_at.asc(), CustomerTaskActivity.id.asc())
        ).all()
        reminder_by_task_id = {
            reminder.task_id: reminder
            for reminder in self.db.scalars(
                select(CustomerTaskEmailReminder).where(
                    CustomerTaskEmailReminder.task_id.in_([task.id for task in tasks])
                )
            )
            if reminder.task_id is not None
        } if tasks else {}
        views: list[CustomerTaskActivityView] = []
        for task in tasks:
            if task.due_at is None:
                continue
            berlin_due_at = task.due_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
            views.append(
                CustomerTaskActivityView(
                    id=task.id,
                    name=task.name,
                    status=task.status,
                    due_at=task.due_at,
                    due_date=berlin_due_at.strftime("%Y-%m-%d"),
                    due_time=berlin_due_at.strftime("%H:%M"),
                    reminder_channel=task.reminder_channel,
                    reminder_minutes_before=task.reminder_minutes_before,
                    email_reminder_status=reminder_by_task_id.get(task.id).status if task.id in reminder_by_task_id else None,
                    email_reminder_last_error=reminder_by_task_id.get(task.id).last_error if task.id in reminder_by_task_id else None,
                    description=task.description,
                )
            )
        return tuple(views)

    def list_meetings(self, *, customer_id: int) -> tuple[CustomerMeetingActivityView, ...]:
        self.complete_elapsed_meetings()
        meetings = self.db.scalars(
            select(CustomerMeetingActivity)
            .options(selectinload(CustomerMeetingActivity.reminders))
            .where(CustomerMeetingActivity.customer_id == customer_id)
            .order_by(CustomerMeetingActivity.starts_at.asc(), CustomerMeetingActivity.id.asc())
        ).all()
        views: list[CustomerMeetingActivityView] = []
        for meeting in meetings:
            if meeting.starts_at is None or meeting.ends_at is None:
                continue
            berlin_start = meeting.starts_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
            berlin_end = meeting.ends_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
            views.append(
                CustomerMeetingActivityView(
                    id=meeting.id,
                    name=meeting.name,
                    status=meeting.status,
                    starts_at=meeting.starts_at,
                    start_date=berlin_start.strftime("%Y-%m-%d"),
                    start_time=berlin_start.strftime("%H:%M"),
                    end_date=berlin_end.strftime("%Y-%m-%d"),
                    end_time=berlin_end.strftime("%H:%M"),
                    duration_minutes=int((meeting.ends_at - meeting.starts_at).total_seconds() // 60),
                    reminders=tuple(
                        CustomerMeetingReminderView(channel=reminder.channel, minutes_before=reminder.minutes_before)
                        for reminder in meeting.reminders
                    ),
                    description=meeting.description,
                )
            )
        return tuple(views)

    def list_calendar_activities(self, *, week_start: date) -> tuple[CustomerCalendarActivityView, ...]:
        """Return calls and meetings that start in the requested Berlin calendar week."""
        self.complete_elapsed_meetings()
        berlin = ZoneInfo("Europe/Berlin")
        week_end = week_start + timedelta(days=7)
        starts_at = datetime.combine(week_start, time.min, tzinfo=berlin).astimezone(UTC).replace(tzinfo=None)
        ends_at = datetime.combine(week_end, time.min, tzinfo=berlin).astimezone(UTC).replace(tzinfo=None)
        customer_names: dict[int, str] = {}

        def customer_name(customer_id: int | None) -> str:
            if customer_id is None:
                return ""
            name = customer_names.get(customer_id)
            if name is None:
                customer = self.db.get(Customer, customer_id)
                name = customer.name if customer is not None else "Unbekannter Kunde"
                customer_names[customer_id] = name
            return name

        calls = self.db.scalars(
            select(CustomerCallActivity)
            .options(selectinload(CustomerCallActivity.reminders))
            .where(CustomerCallActivity.starts_at >= starts_at)
            .where(CustomerCallActivity.starts_at < ends_at)
            .order_by(CustomerCallActivity.starts_at.asc(), CustomerCallActivity.id.asc())
        ).all()
        meetings = self.db.scalars(
            select(CustomerMeetingActivity)
            .options(selectinload(CustomerMeetingActivity.reminders))
            .where(CustomerMeetingActivity.starts_at.is_not(None))
            .where(CustomerMeetingActivity.starts_at >= starts_at)
            .where(CustomerMeetingActivity.starts_at < ends_at)
            .order_by(CustomerMeetingActivity.starts_at.asc(), CustomerMeetingActivity.id.asc())
        ).all()
        views: list[CustomerCalendarActivityView] = []
        for call in calls:
            reminders = tuple(
                CustomerCallReminderView(channel=reminder.channel, minutes_before=reminder.minutes_before)
                for reminder in call.reminders
            )
            if not reminders and call.reminder_channel in {"popup", "email"} and call.reminder_minutes_before is not None:
                reminders = (CustomerCallReminderView(call.reminder_channel, call.reminder_minutes_before),)
            views.append(
                self._calendar_activity_view(
                    id=call.id,
                    kind="call",
                    name=call.name,
                    status=call.status,
                    customer_id=call.customer_id,
                    customer_name=customer_name(call.customer_id),
                    direction=call.direction,
                    starts_at=call.starts_at,
                    ends_at=call.ends_at,
                    reminders=reminders,
                    description=call.description,
                )
            )
        for meeting in meetings:
            if meeting.starts_at is None or meeting.ends_at is None:
                continue
            views.append(
                self._calendar_activity_view(
                    id=meeting.id,
                    kind="meeting",
                    name=meeting.name,
                    status=meeting.status,
                    customer_id=meeting.customer_id,
                    customer_name=customer_name(meeting.customer_id),
                    direction=None,
                    starts_at=meeting.starts_at,
                    ends_at=meeting.ends_at,
                    reminders=tuple(
                        CustomerMeetingReminderView(channel=reminder.channel, minutes_before=reminder.minutes_before)
                        for reminder in meeting.reminders
                    ),
                    description=meeting.description,
                )
            )
        ordered_views = tuple(sorted(views, key=lambda activity: (activity.start_date, activity.start_slot, activity.kind, activity.id)))
        return self._layout_calendar_activities(ordered_views)

    def schedule_call(
        self,
        *,
        customer_id: int | None,
        actor: str,
        name: str,
        status: str,
        direction: str,
        start_date: str,
        start_time: str,
        duration_minutes: str,
        reminder_channels: list[str],
        reminder_minutes_before: list[str],
        description: str,
    ) -> CustomerCallActivity:
        customer = self._customer_or_error(customer_id) if customer_id is not None else None
        values = self._validated_call_values(
            name=name,
            status=status,
            direction=direction,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
        parsed_reminders = values.reminders
        primary_reminder = parsed_reminders[0] if parsed_reminders else None
        call = CustomerCallActivity(
            customer_id=customer.id if customer is not None else None,
            name=values.name,
            status=values.status,
            direction=values.direction,
            starts_at=values.starts_at,
            ends_at=values.ends_at,
            duration_minutes=values.duration_minutes,
            reminder_channel=primary_reminder.channel if primary_reminder else "none",
            reminder_minutes_before=primary_reminder.minutes_before if primary_reminder else None,
            description=values.description,
            created_by_username=actor,
            reminders=self._reminder_models(parsed_reminders),
        )
        self.db.add(call)
        self.db.flush()
        return call

    def update_call(
        self,
        *,
        customer_id: int,
        call_id: int,
        name: str,
        status: str,
        direction: str,
        start_date: str,
        start_time: str,
        duration_minutes: str,
        reminder_channels: list[str],
        reminder_minutes_before: list[str],
        description: str,
    ) -> CustomerCallActivity:
        call = self._call_or_error(customer_id=customer_id, call_id=call_id)
        values = self._validated_call_values(
            name=name,
            status=status,
            direction=direction,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
        parsed_reminders = values.reminders
        primary_reminder = parsed_reminders[0] if parsed_reminders else None
        call.name = values.name
        call.status = values.status
        call.direction = values.direction
        call.starts_at = values.starts_at
        call.ends_at = values.ends_at
        call.duration_minutes = values.duration_minutes
        call.reminder_channel = primary_reminder.channel if primary_reminder else "none"
        call.reminder_minutes_before = primary_reminder.minutes_before if primary_reminder else None
        call.description = values.description
        call.reminders = self._reminder_models(parsed_reminders)
        self.db.flush()
        return call

    def update_calendar_call(
        self,
        *,
        call_id: int,
        customer_id: int | None,
        name: str,
        status: str,
        direction: str,
        start_date: str,
        start_time: str,
        duration_minutes: str,
        reminder_channels: list[str],
        reminder_minutes_before: list[str],
        description: str,
    ) -> CustomerCallActivity:
        customer = self._customer_or_error(customer_id) if customer_id is not None else None
        call = self._calendar_call_or_error(call_id=call_id)
        values = self._validated_call_values(
            name=name,
            status=status,
            direction=direction,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
        primary_reminder = values.reminders[0] if values.reminders else None
        call.customer_id = customer.id if customer is not None else None
        call.name = values.name
        call.status = values.status
        call.direction = values.direction
        call.starts_at = values.starts_at
        call.ends_at = values.ends_at
        call.duration_minutes = values.duration_minutes
        call.reminder_channel = primary_reminder.channel if primary_reminder else "none"
        call.reminder_minutes_before = primary_reminder.minutes_before if primary_reminder else None
        call.description = values.description
        call.reminders = self._reminder_models(values.reminders)
        self.db.flush()
        return call

    def delete_call(self, *, customer_id: int, call_id: int) -> CustomerCallActivity:
        call = self._call_or_error(customer_id=customer_id, call_id=call_id)
        self.db.delete(call)
        self.db.flush()
        return call

    def complete_call(self, *, customer_id: int, call_id: int) -> CustomerCallActivity:
        call = self._call_or_error(customer_id=customer_id, call_id=call_id)
        if call.status == "planned":
            call.status = "completed"
            self.db.flush()
        return call

    def schedule_task(
        self,
        *,
        customer_id: int,
        actor: str,
        name: str,
        status: str,
        due_date: str,
        due_time: str,
        reminder_channel: str,
        reminder_minutes_before: str,
        description: str,
    ) -> CustomerTaskActivity:
        customer = self._customer_or_error(customer_id)
        values = self._validated_task_values(
            name=name,
            status=status,
            due_date=due_date,
            due_time=due_time,
            reminder_channel=reminder_channel,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
        task = CustomerTaskActivity(customer_id=customer.id, created_by_username=actor, **values)
        self.db.add(task)
        self.db.flush()
        self._sync_task_email_reminder(task)
        return task

    def update_task(
        self,
        *,
        customer_id: int,
        task_id: int,
        name: str,
        status: str,
        due_date: str,
        due_time: str,
        reminder_channel: str,
        reminder_minutes_before: str,
        description: str,
    ) -> CustomerTaskActivity:
        task = self._task_or_error(customer_id=customer_id, task_id=task_id)
        values = self._validated_task_values(
            name=name,
            status=status,
            due_date=due_date,
            due_time=due_time,
            reminder_channel=reminder_channel,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
        task.name = values["name"]
        task.status = values["status"]
        task.due_at = values["due_at"]
        task.reminder_channel = values["reminder_channel"]
        task.reminder_minutes_before = values["reminder_minutes_before"]
        task.description = values["description"]
        self.db.flush()
        self._sync_task_email_reminder(task)
        return task

    def delete_task(self, *, customer_id: int, task_id: int) -> CustomerTaskActivity:
        task = self._task_or_error(customer_id=customer_id, task_id=task_id)
        TaskEmailReminderService(db=self.db).cancel_for_deleted_task(task=task)
        self.db.delete(task)
        self.db.flush()
        return task

    def complete_task(self, *, customer_id: int, task_id: int) -> CustomerTaskActivity:
        task = self._task_or_error(customer_id=customer_id, task_id=task_id)
        if task.status == "planned":
            task.status = "completed"
            self.db.flush()
            self._sync_task_email_reminder(task)
        return task

    def _sync_task_email_reminder(self, task: CustomerTaskActivity) -> None:
        try:
            TaskEmailReminderService(db=self.db).sync_task(task=task)
        except TaskEmailReminderError as exc:
            raise CustomerActivityError(str(exc)) from exc

    def schedule_meeting(
        self,
        *,
        customer_id: int | None,
        actor: str,
        name: str,
        status: str,
        start_date: str,
        start_time: str,
        duration_minutes: str,
        reminder_channels: list[str],
        reminder_minutes_before: list[str],
        description: str,
    ) -> CustomerMeetingActivity:
        customer = self._customer_or_error(customer_id) if customer_id is not None else None
        values = self._validated_meeting_values(
            name=name,
            status=status,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            description=description,
        )
        reminders = self._parse_reminders(reminder_channels, reminder_minutes_before)
        meeting = CustomerMeetingActivity(
            customer_id=customer.id if customer is not None else None,
            created_by_username=actor,
            reminders=self._meeting_reminder_models(reminders),
            **values,
        )
        self.db.add(meeting)
        self.db.flush()
        return meeting

    def update_meeting(
        self,
        *,
        customer_id: int,
        meeting_id: int,
        name: str,
        status: str,
        start_date: str,
        start_time: str,
        duration_minutes: str,
        reminder_channels: list[str],
        reminder_minutes_before: list[str],
        description: str,
    ) -> CustomerMeetingActivity:
        meeting = self._meeting_or_error(customer_id=customer_id, meeting_id=meeting_id)
        values = self._validated_meeting_values(
            name=name,
            status=status,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            description=description,
        )
        meeting.name = values["name"]
        meeting.status = values["status"]
        meeting.starts_at = values["starts_at"]
        meeting.ends_at = values["ends_at"]
        meeting.reminders = self._meeting_reminder_models(self._parse_reminders(reminder_channels, reminder_minutes_before))
        meeting.description = values["description"]
        self.db.flush()
        return meeting

    def update_calendar_meeting(
        self,
        *,
        meeting_id: int,
        customer_id: int | None,
        name: str,
        status: str,
        start_date: str,
        start_time: str,
        duration_minutes: str,
        reminder_channels: list[str],
        reminder_minutes_before: list[str],
        description: str,
    ) -> CustomerMeetingActivity:
        customer = self._customer_or_error(customer_id) if customer_id is not None else None
        meeting = self._calendar_meeting_or_error(meeting_id=meeting_id)
        values = self._validated_meeting_values(
            name=name,
            status=status,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            description=description,
        )
        meeting.customer_id = customer.id if customer is not None else None
        meeting.name = values["name"]
        meeting.status = values["status"]
        meeting.starts_at = values["starts_at"]
        meeting.ends_at = values["ends_at"]
        meeting.reminders = self._meeting_reminder_models(self._parse_reminders(reminder_channels, reminder_minutes_before))
        meeting.description = values["description"]
        self.db.flush()
        return meeting

    def delete_meeting(self, *, customer_id: int, meeting_id: int) -> CustomerMeetingActivity:
        meeting = self._meeting_or_error(customer_id=customer_id, meeting_id=meeting_id)
        self.db.delete(meeting)
        self.db.flush()
        return meeting

    def complete_elapsed_meetings(self, *, now: datetime | None = None) -> int:
        """Persist the completed status once a planned meeting has reached its end time."""
        current = now or datetime.now(UTC)
        current_utc = current.astimezone(UTC) if current.tzinfo is not None else current.replace(tzinfo=UTC)
        result = self.db.execute(
            update(CustomerMeetingActivity)
            .where(CustomerMeetingActivity.status == "planned")
            .where(CustomerMeetingActivity.ends_at.is_not(None))
            .where(CustomerMeetingActivity.ends_at <= current_utc.replace(tzinfo=None))
            .values(status="completed")
        )
        completed = int(result.rowcount or 0)
        if completed:
            self.db.commit()
        return completed

    @staticmethod
    def _call_view(call: CustomerCallActivity) -> CustomerCallActivityView:
        reminders = tuple(
            CustomerCallReminderView(channel=reminder.channel, minutes_before=reminder.minutes_before)
            for reminder in call.reminders
        )
        if not reminders and call.reminder_channel in {"popup", "email"} and call.reminder_minutes_before is not None:
            reminders = (CustomerCallReminderView(call.reminder_channel, call.reminder_minutes_before),)
        berlin_start = call.starts_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
        return CustomerCallActivityView(
            id=call.id,
            name=call.name,
            status=call.status,
            direction=call.direction,
            starts_at=call.starts_at,
            start_date=berlin_start.strftime("%Y-%m-%d"),
            start_time=berlin_start.strftime("%H:%M"),
            ends_at=call.ends_at,
            duration_minutes=call.duration_minutes,
            reminders=reminders,
            description=call.description,
        )

    @staticmethod
    def _calendar_activity_view(
        *,
        id: int,
        kind: str,
        name: str,
        status: str,
        customer_id: int | None,
        customer_name: str,
        direction: str | None,
        starts_at: datetime,
        ends_at: datetime,
        reminders: tuple[CustomerCallReminderView | CustomerMeetingReminderView, ...],
        description: str | None,
    ) -> CustomerCalendarActivityView:
        berlin_start = starts_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
        duration_minutes = max(1, int((ends_at - starts_at).total_seconds() // 60))
        return CustomerCalendarActivityView(
            id=id,
            kind=kind,
            name=name,
            status=status,
            customer_id=customer_id,
            customer_name=customer_name,
            direction=direction,
            start_date=berlin_start.strftime("%Y-%m-%d"),
            start_time=berlin_start.strftime("%H:%M"),
            end_time=(berlin_start + timedelta(minutes=duration_minutes)).strftime("%H:%M"),
            duration_minutes=duration_minutes,
            reminder_channels=tuple(reminder.channel for reminder in reminders),
            reminder_minutes_before=tuple(reminder.minutes_before for reminder in reminders),
            description=description,
            start_slot=(berlin_start.hour * 2) + (berlin_start.minute / 30),
            slot_span=max(1, duration_minutes / 30),
        )

    @staticmethod
    def _layout_calendar_activities(
        activities: tuple[CustomerCalendarActivityView, ...],
    ) -> tuple[CustomerCalendarActivityView, ...]:
        """Place each connected overlap group into the smallest set of event columns."""
        by_day: dict[str, list[CustomerCalendarActivityView]] = {}
        for activity in activities:
            by_day.setdefault(activity.start_date, []).append(activity)

        positioned: list[CustomerCalendarActivityView] = []
        for day_activities in by_day.values():
            overlap_group: list[CustomerCalendarActivityView] = []
            group_end = 0.0
            for activity in day_activities:
                if overlap_group and activity.start_slot >= group_end:
                    positioned.extend(CustomerActivityService._layout_overlap_group(overlap_group))
                    overlap_group = []
                    group_end = 0.0
                overlap_group.append(activity)
                group_end = max(group_end, activity.start_slot + activity.slot_span)
            if overlap_group:
                positioned.extend(CustomerActivityService._layout_overlap_group(overlap_group))
        return tuple(positioned)

    @staticmethod
    def _layout_overlap_group(
        activities: list[CustomerCalendarActivityView],
    ) -> list[CustomerCalendarActivityView]:
        active_columns: list[tuple[float, int]] = []
        assigned_columns: list[int] = []
        column_count = 0
        for activity in activities:
            active_columns = [
                (ends_at, column)
                for ends_at, column in active_columns
                if ends_at > activity.start_slot
            ]
            occupied_columns = {column for _ends_at, column in active_columns}
            column = next((index for index in range(column_count) if index not in occupied_columns), column_count)
            column_count = max(column_count, column + 1)
            active_columns.append((activity.start_slot + activity.slot_span, column))
            assigned_columns.append(column)
        return [
            replace(activity, column_index=assigned_columns[index], column_count=column_count)
            for index, activity in enumerate(activities)
        ]

    def _customer_or_error(self, customer_id: int) -> Customer:
        customer = self.db.get(Customer, customer_id)
        # Activities belong to an explicitly opened customer, including hidden Test-Kunden.
        if customer is None:
            raise CustomerActivityError("Der Kunde wurde nicht gefunden.")
        return customer

    def _call_or_error(self, *, customer_id: int, call_id: int) -> CustomerCallActivity:
        call = self.db.scalar(
            select(CustomerCallActivity)
            .options(selectinload(CustomerCallActivity.reminders))
            .where(CustomerCallActivity.id == call_id, CustomerCallActivity.customer_id == customer_id)
        )
        if call is None:
            raise CustomerActivityError("Der Anruf wurde nicht gefunden.")
        return call

    def _calendar_call_or_error(self, *, call_id: int) -> CustomerCallActivity:
        call = self.db.scalar(
            select(CustomerCallActivity)
            .options(selectinload(CustomerCallActivity.reminders))
            .where(CustomerCallActivity.id == call_id)
        )
        if call is None:
            raise CustomerActivityError("Der Anruf wurde nicht gefunden.")
        return call

    def _task_or_error(self, *, customer_id: int, task_id: int) -> CustomerTaskActivity:
        task = self.db.scalar(
            select(CustomerTaskActivity).where(
                CustomerTaskActivity.id == task_id,
                CustomerTaskActivity.customer_id == customer_id,
            )
        )
        if task is None:
            raise CustomerActivityError("Die Aufgabe wurde nicht gefunden.")
        return task

    def _meeting_or_error(self, *, customer_id: int, meeting_id: int) -> CustomerMeetingActivity:
        meeting = self.db.scalar(
            select(CustomerMeetingActivity)
            .options(selectinload(CustomerMeetingActivity.reminders))
            .where(
                CustomerMeetingActivity.id == meeting_id,
                CustomerMeetingActivity.customer_id == customer_id,
            )
        )
        if meeting is None:
            raise CustomerActivityError("Das Meeting wurde nicht gefunden.")
        return meeting

    def _calendar_meeting_or_error(self, *, meeting_id: int) -> CustomerMeetingActivity:
        meeting = self.db.scalar(
            select(CustomerMeetingActivity)
            .options(selectinload(CustomerMeetingActivity.reminders))
            .where(CustomerMeetingActivity.id == meeting_id)
        )
        if meeting is None:
            raise CustomerActivityError("Das Meeting wurde nicht gefunden.")
        return meeting

    def _validated_task_values(
        self,
        *,
        name: str,
        status: str,
        due_date: str,
        due_time: str,
        reminder_channel: str,
        reminder_minutes_before: str,
        description: str,
    ) -> dict[str, object]:
        normalized_name = name.strip()
        if not normalized_name:
            raise CustomerActivityError("Bitte einen Namen für die Aufgabe eingeben.")
        if len(normalized_name) > 255:
            raise CustomerActivityError("Der Name für die Aufgabe darf höchstens 255 Zeichen lang sein.")
        if status not in dict(CALL_STATUS_OPTIONS):
            raise CustomerActivityError("Bitte einen gültigen Status wählen.")
        channel = reminder_channel.strip().lower()
        if channel not in dict(CALL_REMINDER_CHANNEL_OPTIONS):
            raise CustomerActivityError("Bitte eine gültige Art der Erinnerung wählen.")
        minutes_before = (
            None
            if channel == "none"
            else self._parse_choice(reminder_minutes_before, CALL_REMINDER_OPTIONS, "Erinnerung")
        )
        return {
            "name": normalized_name,
            "status": status,
            "due_at": self._parse_datetime(due_date, due_time, "Startdatum"),
            "reminder_channel": channel,
            "reminder_minutes_before": minutes_before,
            "description": description.strip() or None,
        }

    def _validated_meeting_values(
        self,
        *,
        name: str,
        status: str,
        start_date: str,
        start_time: str,
        duration_minutes: str,
        description: str,
    ) -> dict[str, object]:
        normalized_name = name.strip()
        if not normalized_name:
            raise CustomerActivityError("Bitte einen Namen für das Meeting eingeben.")
        if len(normalized_name) > 255:
            raise CustomerActivityError("Der Name für das Meeting darf höchstens 255 Zeichen lang sein.")
        if status not in dict(CALL_STATUS_OPTIONS):
            raise CustomerActivityError("Bitte einen gültigen Status wählen.")
        starts_at = self._parse_datetime(start_date, start_time, "Startdatum")
        parsed_duration = self._parse_choice(duration_minutes, CALL_DURATION_OPTIONS, "Dauer")
        return {
            "name": normalized_name,
            "status": status,
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(minutes=parsed_duration),
            "description": description.strip() or None,
        }

    def _validated_call_values(
        self,
        *,
        name: str,
        status: str,
        direction: str,
        start_date: str,
        start_time: str,
        duration_minutes: str,
        reminder_channels: list[str],
        reminder_minutes_before: list[str],
        description: str,
    ) -> CustomerCallValues:
        normalized_name = name.strip()
        if not normalized_name:
            raise CustomerActivityError("Bitte einen Namen für den Anruf eingeben.")
        if len(normalized_name) > 255:
            raise CustomerActivityError("Der Name für den Anruf darf höchstens 255 Zeichen lang sein.")
        if status not in dict(CALL_STATUS_OPTIONS):
            raise CustomerActivityError("Bitte einen gültigen Status wählen.")
        if direction not in dict(CALL_DIRECTION_OPTIONS):
            raise CustomerActivityError("Bitte eine gültige Richtung wählen.")

        starts_at = self._parse_datetime(start_date, start_time, "Startdatum")
        parsed_duration = self._parse_choice(duration_minutes, CALL_DURATION_OPTIONS, "Dauer")
        return CustomerCallValues(
            name=normalized_name,
            status=status,
            direction=direction,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=parsed_duration),
            duration_minutes=parsed_duration,
            reminders=self._parse_reminders(reminder_channels, reminder_minutes_before),
            description=description.strip() or None,
        )

    @staticmethod
    def _reminder_models(reminders: tuple[CustomerCallReminderView, ...]) -> list[CustomerCallReminder]:
        return [
            CustomerCallReminder(
                channel=reminder.channel,
                minutes_before=reminder.minutes_before,
                sort_order=index,
            )
            for index, reminder in enumerate(reminders)
        ]

    @staticmethod
    def _meeting_reminder_models(reminders: tuple[CustomerCallReminderView, ...]) -> list[CustomerMeetingReminder]:
        return [
            CustomerMeetingReminder(
                channel=reminder.channel,
                minutes_before=reminder.minutes_before,
                sort_order=index,
            )
            for index, reminder in enumerate(reminders)
        ]

    @staticmethod
    def _parse_datetime(date_value: str, time_value: str, label: str) -> datetime:
        try:
            local_value = datetime.strptime(f"{date_value.strip()} {time_value.strip()}", "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise CustomerActivityError(f"Bitte ein gültiges {label} eingeben.") from exc
        return local_value.replace(tzinfo=ZoneInfo("Europe/Berlin")).astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _parse_choice(value: str, allowed_values: tuple[int, ...], label: str) -> int:
        try:
            parsed_value = int(value)
        except (TypeError, ValueError) as exc:
            raise CustomerActivityError(f"Bitte eine gültige {label} wählen.") from exc
        if parsed_value not in allowed_values:
            raise CustomerActivityError(f"Bitte eine gültige {label} wählen.")
        return parsed_value

    def _parse_reminders(
        self,
        channels: list[str],
        minutes_values: list[str],
    ) -> tuple[CustomerCallReminderView, ...]:
        if len(channels) > 5:
            raise CustomerActivityError("Es können höchstens fünf Erinnerungen geplant werden.")
        allowed_channels = dict(CALL_REMINDER_CHANNEL_OPTIONS)
        reminders: list[CustomerCallReminderView] = []
        for index, raw_channel in enumerate(channels):
            channel = raw_channel.strip().lower()
            if channel not in allowed_channels:
                raise CustomerActivityError("Bitte eine gültige Art der Erinnerung wählen.")
            if channel == "none":
                continue
            if index >= len(minutes_values):
                raise CustomerActivityError("Bitte einen Zeitpunkt für jede Erinnerung wählen.")
            minutes_before = self._parse_choice(minutes_values[index], CALL_REMINDER_OPTIONS, "Erinnerung")
            reminders.append(CustomerCallReminderView(channel=channel, minutes_before=minutes_before))
        return tuple(reminders)
