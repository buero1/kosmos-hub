from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
import math
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload, object_session

from app.core.security import get_secret_cipher
from app.services.hub_record_info import record_info
from app.models.customer import Customer
from app.models.hub_case import HubCase
from app.models.hub_lead import HubLead
from app.models.hub_user import HubUser
from app.services.hub_activity_responsibility import ActivityUserMetadata, initial_responsibility
from app.models.customer_activity import CustomerCallActivity, CustomerCallReminder, CustomerMeetingActivity, CustomerMeetingReminder, CustomerTaskActivity
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.services.task_email_reminders import TaskEmailReminderError, TaskEmailReminderService
from app.services.hub_leads import HubLeadService


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


def activity_creation_metadata(activity) -> dict[str, object]:
    return {key: value for key, value in activity_metadata(activity).items() if key in {"created_by", "created_at"}}


def activity_metadata(activity) -> dict[str, object]:
    info = record_info(activity)
    db = object_session(activity)
    creator = db.get(HubUser, activity.created_by_user_id) if db and activity.created_by_user_id else None
    assignee = db.get(HubUser, activity.assignee_user_id) if db and activity.assignee_user_id else None
    author = info["created_by"]
    if author == "Nicht bekannt":
        author = creator.display_name if creator else (activity.created_by_username or author)
    return {"created_by": author, "created_at": info["created_at"],
            "assignee_user_id": activity.assignee_user_id,
            "assignee_name": (assignee.display_name + (" (inaktiv)" if not assignee.is_active else "")) if assignee else "Nicht zugeordnet",
            "changed_by": info["changed_by"], "changed_at": info["changed_at"],
            "completed_by": activity.completed_by_name or "", "completed_at": activity.completed_at}


@dataclass(frozen=True)
class CustomerCallActivityView(ActivityUserMetadata):
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
    duration_seconds: int | None
    recording_url: str | None
    transcript_url: str | None
    created_by: str = ""
    created_at: datetime | None = None


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
class CustomerTaskActivityView(ActivityUserMetadata):
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
    created_by: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class CustomerMeetingActivityView(ActivityUserMetadata):
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
    created_by: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class CustomerMeetingReminderView:
    channel: str
    minutes_before: int


@dataclass(frozen=True)
class CustomerCalendarActivityView(ActivityUserMetadata):
    """A call or meeting positioned in the Berlin-time weekly calendar."""

    id: int
    kind: str
    name: str
    status: str
    customer_id: int | None
    customer_name: str
    lead_id: int | None
    lead_name: str
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
    created_by: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class CustomerActivityDirectoryEntry(ActivityUserMetadata):
    id: int
    kind: str
    name: str
    status: str
    status_label: str
    direction_label: str
    scheduled_at: datetime | None
    ends_at: datetime | None
    duration_minutes: int | None
    reminder_label: str
    direction: str
    scheduled_date: str
    scheduled_time: str
    reminder_channels: tuple[str, ...]
    reminder_minutes_before: tuple[int, ...]
    task_reminder_channel: str
    task_reminder_minutes_before: int
    description: str
    related_name: str
    related_kind: str
    related_href: str
    relation_label: str
    activity_href: str
    update_href: str
    created_by_username: str
    created_at: datetime | None = None

    @property
    def created_by(self) -> str:
        return self.created_by_username


class CustomerActivityService:
    def __init__(self, *, db: Session):
        self.db = db

    def list_calls(
        self,
        *,
        customer_id: int | None = None,
        lead_id: int | None = None,
        include_completed: bool = False,
    ) -> tuple[CustomerCallActivityView, ...]:
        owner_filter = self._owner_filter(CustomerCallActivity, customer_id=customer_id, lead_id=lead_id)
        query = select(CustomerCallActivity).options(selectinload(CustomerCallActivity.reminders)).where(owner_filter)
        query = query.where(
            CustomerCallActivity.status.in_(("planned", "completed"))
            if include_completed
            else CustomerCallActivity.status == "planned"
        ).order_by(CustomerCallActivity.starts_at.desc(), CustomerCallActivity.id.desc())
        calls = self.db.scalars(query).all()
        return tuple(
            self._call_view(call)
            for call in calls
        )

    def list_tasks(self, *, customer_id: int | None = None, lead_id: int | None = None) -> tuple[CustomerTaskActivityView, ...]:
        owner_filter = self._owner_filter(CustomerTaskActivity, customer_id=customer_id, lead_id=lead_id)
        tasks = self.db.scalars(
            select(CustomerTaskActivity)
            .where(owner_filter)
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
                    **activity_metadata(task),
                )
            )
        return tuple(views)

    def list_meetings(self, *, customer_id: int | None = None, lead_id: int | None = None) -> tuple[CustomerMeetingActivityView, ...]:
        owner_filter = self._owner_filter(CustomerMeetingActivity, customer_id=customer_id, lead_id=lead_id)
        meetings = self.db.scalars(
            select(CustomerMeetingActivity)
            .options(selectinload(CustomerMeetingActivity.reminders))
            .where(owner_filter)
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
                    **activity_metadata(meeting),
                )
            )
        return tuple(views)

    def list_directory_entries(self, *, kind: str) -> tuple[CustomerActivityDirectoryEntry, ...]:
        models = {
            "call": CustomerCallActivity,
            "task": CustomerTaskActivity,
            "meeting": CustomerMeetingActivity,
        }
        model = models.get(kind)
        if model is None:
            raise CustomerActivityError("Die Aktivitätsart ist ungültig.")

        query = select(model)
        if kind in {"call", "meeting"}:
            query = query.options(selectinload(model.reminders))
        activities = list(self.db.scalars(query).all())
        planned = sorted(
            (activity for activity in activities if activity.status == "planned"),
            key=lambda activity: (self._directory_scheduled_at(activity) or datetime.max, activity.id),
        )
        finished = sorted(
            (activity for activity in activities if activity.status != "planned"),
            key=lambda activity: (self._directory_scheduled_at(activity) or datetime.min, activity.id),
            reverse=True,
        )
        activities = [*planned, *finished]

        customer_ids = {activity.customer_id for activity in activities if activity.customer_id is not None}
        customer_names = {
            customer_id: customer_name
            for customer_id, customer_name in self.db.execute(
                select(Customer.id, Customer.name).where(Customer.id.in_(customer_ids))
            )
        } if customer_ids else {}
        lead_ids = {activity.lead_id for activity in activities if activity.lead_id is not None}
        lead_names = {
            entry.lead.id: entry.name
            for entry in HubLeadService(db=self.db, cipher=get_secret_cipher()).list_leads()
            if entry.lead.id in lead_ids
        } if lead_ids else {}
        case_ids = {
            activity.case_id
            for activity in activities
            if isinstance(activity, CustomerTaskActivity) and activity.case_id is not None
        }
        case_names = {
            case.id: case.case_number or f"Fall {case.id}"
            for case in self.db.scalars(select(HubCase).where(HubCase.id.in_(case_ids))).all()
        } if case_ids else {}

        return tuple(
            self._directory_entry(
                activity=activity,
                kind=kind,
                customer_names=customer_names,
                lead_names=lead_names,
                case_names=case_names,
            )
            for activity in activities
        )

    def list_calendar_activities(self, *, week_start: date, allowed_call_ids: set[int] | None = None,
                                 allowed_meeting_ids: set[int] | None = None,
                                 persist_elapsed: bool = True) -> tuple[CustomerCalendarActivityView, ...]:
        """Return calls and meetings that start in the requested Berlin calendar week."""
        if persist_elapsed:
            self.complete_elapsed_meetings()
        berlin = ZoneInfo("Europe/Berlin")
        week_end = week_start + timedelta(days=7)
        starts_at = datetime.combine(week_start, time.min, tzinfo=berlin).astimezone(UTC).replace(tzinfo=None)
        ends_at = datetime.combine(week_end, time.min, tzinfo=berlin).astimezone(UTC).replace(tzinfo=None)
        customer_names: dict[int, str] = {}
        lead_names: dict[int, str] = {}

        def customer_name(customer_id: int | None) -> str:
            if customer_id is None:
                return ""
            name = customer_names.get(customer_id)
            if name is None:
                customer = self.db.get(Customer, customer_id)
                name = customer.name if customer is not None else "Unbekannter Kunde"
                customer_names[customer_id] = name
            return name

        def lead_name(lead_id: int | None) -> str:
            if lead_id is None:
                return ""
            name = lead_names.get(lead_id)
            if name is None:
                lead = HubLeadService(db=self.db, cipher=get_secret_cipher()).get_detail(lead_id=lead_id)
                name = lead.name if lead is not None else f"Lead {lead_id}"
                lead_names[lead_id] = name
            return name

        calls = self.db.scalars(
            select(CustomerCallActivity)
            .options(selectinload(CustomerCallActivity.reminders))
            .where(CustomerCallActivity.starts_at >= starts_at)
            .where(CustomerCallActivity.starts_at < ends_at)
            .where(CustomerCallActivity.id.in_(allowed_call_ids) if allowed_call_ids is not None else True)
            .order_by(CustomerCallActivity.starts_at.asc(), CustomerCallActivity.id.asc())
        ).all()
        meetings = self.db.scalars(
            select(CustomerMeetingActivity)
            .options(selectinload(CustomerMeetingActivity.reminders))
            .where(CustomerMeetingActivity.starts_at.is_not(None))
            .where(CustomerMeetingActivity.starts_at >= starts_at)
            .where(CustomerMeetingActivity.starts_at < ends_at)
            .where(CustomerMeetingActivity.id.in_(allowed_meeting_ids) if allowed_meeting_ids is not None else True)
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
                    lead_id=call.lead_id,
                    lead_name=lead_name(call.lead_id),
                    direction=call.direction,
                    starts_at=call.starts_at,
                    ends_at=call.ends_at,
                    reminders=reminders,
                    description=call.description,
                    **activity_metadata(call),
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
                    status=("completed" if not persist_elapsed and meeting.status == "planned"
                            and meeting.ends_at.replace(tzinfo=UTC) <= datetime.now(UTC) else meeting.status),
                    customer_id=meeting.customer_id,
                    customer_name=customer_name(meeting.customer_id),
                    lead_id=meeting.lead_id,
                    lead_name=lead_name(meeting.lead_id),
                    direction=None,
                    starts_at=meeting.starts_at,
                    ends_at=meeting.ends_at,
                    reminders=tuple(
                        CustomerMeetingReminderView(channel=reminder.channel, minutes_before=reminder.minutes_before)
                        for reminder in meeting.reminders
                    ),
                    description=meeting.description,
                    **activity_metadata(meeting),
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
        lead_id: int | None = None,
        assignee_user_id: int | None = None,
    ) -> CustomerCallActivity:
        if lead_id is not None and (customer_id is not None or self.db.get(HubLead, lead_id) is None):
            raise CustomerActivityError("Der Lead wurde nicht gefunden oder die Verknüpfung ist ungültig.")
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
            lead_id=lead_id,
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
            **initial_responsibility(self.db, actor, assignee_user_id),
            reminders=self._reminder_models(parsed_reminders),
        )
        self.db.add(call)
        self.db.flush()
        return call

    def upsert_external_call(
        self,
        *,
        lead_id: int,
        source_system: str,
        source_external_id: str,
        actor: str,
        name: str,
        status: str,
        direction: str,
        starts_at: datetime,
        duration_seconds: int,
        description: str = "",
        recording_url: str = "",
        transcript_url: str = "",
        reminder_channels: list[str] | None = None,
        reminder_minutes_before: list[str] | None = None,
    ) -> CustomerCallActivity:
        if self.db.get(HubLead, lead_id) is None:
            raise CustomerActivityError("Der Lead wurde nicht gefunden.")
        normalized_source = source_system.strip()[:96]
        normalized_external_id = source_external_id.strip()[:255]
        normalized_name = name.strip()[:255]
        if not normalized_source or not normalized_external_id or not normalized_name:
            raise CustomerActivityError("Externe Quelle, Anruf-ID und Name dürfen nicht leer sein.")
        if status not in dict(CALL_STATUS_OPTIONS) or direction not in dict(CALL_DIRECTION_OPTIONS):
            raise CustomerActivityError("Anrufstatus oder Richtung ist ungültig.")
        normalized_start = starts_at.astimezone(UTC).replace(tzinfo=None) if starts_at.tzinfo is not None else starts_at
        normalized_duration = max(0, min(int(duration_seconds or 0), 24 * 60 * 60))
        stored_duration_minutes = max(1, math.ceil(normalized_duration / 60))
        parsed_reminders = self._parse_reminders(reminder_channels or [], reminder_minutes_before or [])
        primary_reminder = parsed_reminders[0] if parsed_reminders else None
        call = self.db.scalar(
            select(CustomerCallActivity).where(
                CustomerCallActivity.source_system == normalized_source,
                CustomerCallActivity.source_external_id == normalized_external_id,
            )
        )
        if call is None:
            call = CustomerCallActivity(
                customer_id=None,
                lead_id=lead_id,
                source_system=normalized_source,
                source_external_id=normalized_external_id,
                created_by_username=actor.strip()[:64] or "integration:callapp",
                **initial_responsibility(self.db, actor),
            )
            self.db.add(call)
        elif call.lead_id != lead_id:
            raise CustomerActivityError("Die externe Anruf-ID gehört bereits zu einem anderen Lead.")
        call.name = normalized_name
        call.status = status
        call.direction = direction
        call.starts_at = normalized_start
        call.ends_at = normalized_start + timedelta(seconds=normalized_duration)
        call.duration_seconds = normalized_duration
        call.duration_minutes = stored_duration_minutes
        call.reminder_channel = primary_reminder.channel if primary_reminder else "none"
        call.reminder_minutes_before = primary_reminder.minutes_before if primary_reminder else None
        call.description = description.strip()[:20_000] or None
        call.recording_url = recording_url.strip()[:4000] or None
        call.transcript_url = transcript_url.strip()[:4000] or None
        call.reminders = self._reminder_models(parsed_reminders)
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

    def update_directory_call(self, *, call_id: int, **values) -> CustomerCallActivity:
        call = self._calendar_call_or_error(call_id=call_id)
        return self.update_calendar_call(call_id=call.id, customer_id=call.customer_id, **values)

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
        customer_id: int | None,
        actor: str,
        name: str,
        status: str,
        due_date: str,
        due_time: str,
        reminder_channel: str,
        reminder_minutes_before: str,
        description: str,
        lead_id: int | None = None,
        assignee_user_id: int | None = None,
    ) -> CustomerTaskActivity:
        if customer_id is not None and lead_id is not None:
            raise CustomerActivityError("Bitte nur einen Kunden oder Lead verknüpfen.")
        customer = self._customer_or_error(customer_id) if customer_id is not None else None
        if lead_id is not None and self.db.get(HubLead, lead_id) is None:
            raise CustomerActivityError("Der Lead wurde nicht gefunden.")
        values = self._validated_task_values(
            name=name,
            status=status,
            due_date=due_date,
            due_time=due_time,
            reminder_channel=reminder_channel,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
        task = CustomerTaskActivity(customer_id=customer.id if customer else None, lead_id=lead_id, created_by_username=actor,
                                    **initial_responsibility(self.db, actor, assignee_user_id), **values)
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

    def update_directory_task(
        self,
        *,
        task_id: int,
        name: str,
        status: str,
        due_date: str,
        due_time: str,
        reminder_channel: str,
        reminder_minutes_before: str,
        description: str,
    ) -> CustomerTaskActivity:
        task = self.db.get(CustomerTaskActivity, task_id)
        if task is None:
            raise CustomerActivityError("Die Aufgabe wurde nicht gefunden.")
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
        lead_id: int | None = None,
        assignee_user_id: int | None = None,
    ) -> CustomerMeetingActivity:
        if lead_id is not None and (customer_id is not None or self.db.get(HubLead, lead_id) is None):
            raise CustomerActivityError("Der Lead wurde nicht gefunden oder die Verknüpfung ist ungültig.")
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
            lead_id=lead_id,
            created_by_username=actor,
            **initial_responsibility(self.db, actor, assignee_user_id),
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

    def update_directory_meeting(self, *, meeting_id: int, **values) -> CustomerMeetingActivity:
        meeting = self._calendar_meeting_or_error(meeting_id=meeting_id)
        return self.update_calendar_meeting(
            meeting_id=meeting.id,
            customer_id=meeting.customer_id,
            **values,
        )

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
            .values(status="completed", completed_at=current_utc.replace(tzinfo=None), completed_by_name="System")
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
            duration_seconds=call.duration_seconds,
            recording_url=call.recording_url,
            transcript_url=call.transcript_url,
            **activity_metadata(call),
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
        lead_id: int | None,
        lead_name: str,
        direction: str | None,
        starts_at: datetime,
        ends_at: datetime,
        reminders: tuple[CustomerCallReminderView | CustomerMeetingReminderView, ...],
        description: str | None,
        created_by: str = "",
        created_at: datetime | None = None,
        **responsibility,
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
            lead_id=lead_id,
            lead_name=lead_name,
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
            created_by=created_by,
            created_at=created_at,
            **responsibility,
        )

    @staticmethod
    def _directory_scheduled_at(
        activity: CustomerCallActivity | CustomerTaskActivity | CustomerMeetingActivity,
    ) -> datetime | None:
        return activity.due_at if isinstance(activity, CustomerTaskActivity) else activity.starts_at

    @staticmethod
    def _directory_reminder_label(activity: CustomerTaskActivity) -> str:
        channel_labels = {"popup": "Popup", "email": "E-Mail", "none": "Keine Erinnerung"}
        channel = activity.reminder_channel or "none"
        if channel == "none" or activity.reminder_minutes_before is None:
            return channel_labels["none"]
        timing = (
            "zum Termin"
            if activity.reminder_minutes_before == 0
            else f"{activity.reminder_minutes_before} Min. vorher"
        )
        return f"{channel_labels.get(channel, channel)} · {timing}"

    @classmethod
    def _directory_entry(
        cls,
        *,
        activity: CustomerCallActivity | CustomerTaskActivity | CustomerMeetingActivity,
        kind: str,
        customer_names: dict[int, str],
        lead_names: dict[int, str],
        case_names: dict[int, str],
    ) -> CustomerActivityDirectoryEntry:
        related_name = related_kind = related_href = ""
        if isinstance(activity, CustomerTaskActivity) and activity.case_id is not None:
            related_name = case_names.get(activity.case_id, f"Fall {activity.case_id}")
            related_kind = "Fall"
            related_href = f"/cases/{activity.case_id}"
        elif activity.customer_id is not None:
            related_name = customer_names.get(activity.customer_id, f"Customer {activity.customer_id}")
            related_kind = "Kunde"
            related_href = f"/customers/{activity.customer_id}"
        elif activity.lead_id is not None:
            related_name = lead_names.get(activity.lead_id, f"Lead {activity.lead_id}")
            related_kind = "Lead"
            related_href = f"/leads/{activity.lead_id}"

        scheduled_at = cls._directory_scheduled_at(activity)
        scheduled_local = (
            scheduled_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
            if scheduled_at is not None
            else None
        )
        ends_at = activity.ends_at if not isinstance(activity, CustomerTaskActivity) else None
        duration_minutes = None
        reminder_channels: tuple[str, ...] = ()
        reminder_minutes_before: tuple[int, ...] = ()
        task_reminder_channel = "none"
        task_reminder_minutes_before = 0
        if isinstance(activity, CustomerCallActivity):
            duration_minutes = activity.duration_minutes
            reminders = tuple((reminder.channel, reminder.minutes_before) for reminder in activity.reminders)
            if not reminders and activity.reminder_channel in {"popup", "email"} and activity.reminder_minutes_before is not None:
                reminders = ((activity.reminder_channel, activity.reminder_minutes_before),)
            reminder_channels = tuple(channel for channel, _ in reminders)
            reminder_minutes_before = tuple(minutes for _, minutes in reminders)
        elif isinstance(activity, CustomerTaskActivity):
            task_reminder_channel = activity.reminder_channel or "none"
            task_reminder_minutes_before = activity.reminder_minutes_before or 0
        elif isinstance(activity, CustomerMeetingActivity) and activity.starts_at is not None and activity.ends_at is not None:
            duration_minutes = max(1, int((activity.ends_at - activity.starts_at).total_seconds() // 60))
            reminder_channels = tuple(reminder.channel for reminder in activity.reminders)
            reminder_minutes_before = tuple(reminder.minutes_before for reminder in activity.reminders)

        relation_label = f"{related_kind} · {related_name}" if related_name else "Keine Verknüpfung"
        creation = activity_metadata(activity)

        return CustomerActivityDirectoryEntry(
            id=activity.id,
            kind=kind,
            name=activity.name,
            status=activity.status,
            status_label={"planned": "Geplant", "completed": "Abgeschlossen", "cancelled": "Abgesagt"}.get(
                activity.status,
                activity.status,
            ),
            direction_label=(
                {"outbound": "Ausgehend", "inbound": "Eingehend"}.get(activity.direction, activity.direction)
                if isinstance(activity, CustomerCallActivity)
                else ""
            ),
            scheduled_at=scheduled_at,
            ends_at=ends_at,
            duration_minutes=duration_minutes,
            reminder_label=cls._directory_reminder_label(activity) if isinstance(activity, CustomerTaskActivity) else "",
            direction=activity.direction if isinstance(activity, CustomerCallActivity) else "",
            scheduled_date=scheduled_local.strftime("%Y-%m-%d") if scheduled_local else "",
            scheduled_time=scheduled_local.strftime("%H:%M") if scheduled_local else "",
            reminder_channels=reminder_channels,
            reminder_minutes_before=reminder_minutes_before,
            task_reminder_channel=task_reminder_channel,
            task_reminder_minutes_before=task_reminder_minutes_before,
            description=activity.description or "",
            related_name=related_name,
            related_kind=related_kind,
            related_href=related_href,
            relation_label=relation_label,
            activity_href=f"/activities/{kind}/{activity.id}",
            update_href=f"/activities/{kind}/{activity.id}",
            created_by_username=creation["created_by"],
            created_at=creation["created_at"],
            **{key: value for key, value in creation.items() if key not in {"created_by", "created_at"}},
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

    @staticmethod
    def _owner_filter(model, *, customer_id: int | None, lead_id: int | None):
        if (customer_id is None) == (lead_id is None):
            raise CustomerActivityError("Bitte genau einen Kunden oder Lead auswählen.")
        return model.customer_id == customer_id if customer_id is not None else model.lead_id == lead_id

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
