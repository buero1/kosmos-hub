from datetime import UTC, datetime
from zoneinfo import ZoneInfo


BERLIN_TIMEZONE = ZoneInfo("Europe/Berlin")


def berlin_datetime(value: datetime) -> datetime:
    """DB timestamps without an offset are UTC, never server-local time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BERLIN_TIMEZONE)


def iso_berlin_time(value: datetime) -> str:
    """Keep query timestamps machine-readable, with an explicit Hub UTC offset."""
    return berlin_datetime(value).isoformat()


def format_berlin_time(value: object) -> str:
    """Render stored UTC values for people using the Hub in Berlin."""
    return _format_berlin_time(value, "%d.%m.%Y %H:%M:%S %Z")


def format_berlin_time_local(value: object) -> str:
    """Keep seconds and Berlin local time without a timezone suffix."""
    return _format_berlin_time(value, "%d.%m.%Y %H:%M:%S")


def format_berlin_time_short(value: object) -> str:
    """Render concise Berlin times for dense mailbox lists."""
    return _format_berlin_time(value, "%d.%m.%Y %H:%M")


def _format_berlin_time(value: object, pattern: str) -> str:
    if value is None:
        return "-"

    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return "-"
        try:
            value = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return normalized

    if not isinstance(value, datetime):
        return str(value)

    return berlin_datetime(value).strftime(pattern)
