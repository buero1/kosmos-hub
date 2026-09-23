"""Discoverable calendar reads; creation/editing remain shared activity operations."""
from dataclasses import asdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from app.core.timezones import iso_berlin_time

from app.services.hub_calendar import calendar_activities, busy_times
from app.services.hub_activity_responsibility import ACTIVITY_VIEWS
from app.services.hub_operation_crm_reads import page
from app.services.hub_operations import HubQuery, HubOperationError, HubOperationInputField as Field, register_query


def week(service, values):
    try:
        selected = date.fromisoformat(values["week"]) if values.get("week") else datetime.now(ZoneInfo("Europe/Berlin")).date()
        start = selected - timedelta(days=selected.weekday())
        end = start + timedelta(days=7)
    except (ValueError, OverflowError) as exc:
        raise HubOperationError("Ungueltige Kalenderwoche.") from exc
    events = [{**{key: iso_berlin_time(value) if isinstance(value, datetime) else value for key, value in asdict(item).items()}, "activity_id": str(item.id), "description": (item.description or "")[:1000],
               "created_at": iso_berlin_time(item.created_at) if item.created_at else "",
               "description_truncated": len(item.description or "") > 1000}
              for item in calendar_activities(service, start, view=values.get("view") or "all")]
    return {**page(events, values), "week_start": start.isoformat(), "week_end_exclusive": end.isoformat(), "timezone": "Europe/Berlin"}


def busy(service, values):
    try:
        start, end = (datetime.fromisoformat(values[name]) for name in ("start", "end"))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Timezone required")
    except ValueError as exc:
        raise HubOperationError("Start und Ende als ISO-Datum mit Zeitzone angeben.") from exc
    return {**page(busy_times(service, start, end), values), "timezone": "UTC",
            "notice": "Nur sichtbare geplante Anrufe und Meetings; keine Reservierung oder Garantie fuer unbekannte Kalender."}


register_query(HubQuery("calendar.week", "Kalenderwoche wie in der Maske lesen. Nur sichtbare Termine, 25 pro Seite, keine Statusaenderung. Bei description_truncated den vollstaendigen Text ueber activities.calls.read bzw. activities.meetings.read nachladen.",
    (Field("week", "Datum innerhalb der Woche (YYYY-MM-DD), sonst aktuelle Woche"), Field("view", "Benutzeransicht", options=ACTIVITY_VIEWS), Field("offset", "Seitenbeginn")), week))
register_query(HubQuery("calendar.busy_times", "Sichtbare geplante Anrufe/Meetings mit Ueberschneidung zum Zeitraum lesen, maximal 120 Tage. Keine Fremdkalender oder Schreibaktion.",
    (Field("start", "Beginn als ISO-Datum mit UTC-Offset", required=True), Field("end", "Ende als ISO-Datum mit UTC-Offset", required=True), Field("offset", "Seitenbeginn")), busy))
