from datetime import UTC, datetime
from zoneinfo import ZoneInfo


BERLIN_TIMEZONE = ZoneInfo("Europe/Berlin")


def format_berlin_time(value: object) -> str:
    """Render stored UTC values for people using the Hub in Berlin."""
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

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BERLIN_TIMEZONE).strftime("%d.%m.%Y %H:%M:%S %Z")
