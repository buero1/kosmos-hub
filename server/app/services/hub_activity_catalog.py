"""Activity fields and defaults shared by the drawers and operation catalog."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.customer_activities import (
    CALL_DIRECTION_OPTIONS, CALL_DURATION_OPTIONS, CALL_REMINDER_CHANNEL_OPTIONS,
    CALL_REMINDER_OPTIONS, CALL_STATUS_OPTIONS, suggested_call_start,
)
from app.services.hub_operations import HubOperationInputField


ACTIVITY_KINDS = {"task": ("tasks", "Aufgabe"), "call": ("calls", "Anruf"), "meeting": ("meetings", "Meeting")}


def activity_fields(kind: str) -> tuple[HubOperationInputField, ...]:
    fields = (
        HubOperationInputField("name", "Name", required=True),
        HubOperationInputField("status", "Status", required=True, options=CALL_STATUS_OPTIONS),
        HubOperationInputField("description", "Beschreibung"),
        HubOperationInputField("assignee_user_id", "Verantwortliche Person (Benutzer-ID)"),
    )
    prefix = "due" if kind == "task" else "start"
    fields += (
        HubOperationInputField(f"{prefix}_date", "Startdatum", required=True),
        HubOperationInputField(f"{prefix}_time", "Uhrzeit", required=True),
        HubOperationInputField(
            "reminder_channel" if kind == "task" else "reminder_channels", "Erinnerung",
            options=CALL_REMINDER_CHANNEL_OPTIONS, multiple=kind != "task",
        ),
        HubOperationInputField("reminder_minutes_before", "Minuten vorher", options=tuple(
            (str(value), str(value)) for value in CALL_REMINDER_OPTIONS
        ), multiple=kind != "task"),
    )
    if kind != "task":
        fields += (HubOperationInputField("duration_minutes", "Dauer in Minuten", required=True, options=tuple(
            (str(value), str(value)) for value in CALL_DURATION_OPTIONS
        )),)
    if kind == "call":
        fields += (HubOperationInputField("direction", "Richtung", required=True, options=CALL_DIRECTION_OPTIONS),)
    return fields


def activity_form_defaults(*, now: datetime | None = None, start: datetime | None = None) -> dict:
    now = now or datetime.now(ZoneInfo("Europe/Berlin"))
    start = start or suggested_call_start(now)
    shared = {"status": "planned", "description": ""}
    timed = {**shared, "start_date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M")}
    return {
        "call_defaults": {
            **timed, "direction": "outbound", "duration_minutes": 30,
            "reminder_channel": "popup", "reminder_minutes_before": 5,
        },
        "task_defaults": {
            **shared, "due_date": (now.date() + timedelta(days=1)).isoformat(), "due_time": "09:00",
            "reminder_channel": "email", "reminder_minutes_before": 0,
        },
        "meeting_defaults": {
            **timed, "duration_minutes": 60,
            "end_date": (start + timedelta(minutes=60)).strftime("%Y-%m-%d"),
            "end_time": (start + timedelta(minutes=60)).strftime("%H:%M"),
            "reminder_channel": "popup", "reminder_minutes_before": 15,
        },
    }


def activity_defaults(kind: str) -> dict[str, str]:
    values = {key: str(value) for key, value in activity_form_defaults()[f"{kind}_defaults"].items()}
    if kind != "task":
        values["reminder_channels"] = values.pop("reminder_channel")
        values.pop("end_date", None)
        values.pop("end_time", None)
    return values
