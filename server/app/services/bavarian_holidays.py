"""Calendar dates for Bavaria-wide statutory public holidays."""

from __future__ import annotations

from datetime import date, timedelta


def bavarian_public_holidays(year: int) -> dict[date, str]:
    """Return Bavaria-wide statutory public holidays for *year*.

    Mariä Himmelfahrt and the Augsburger Friedensfest are local holidays. They
    require a configured municipality and are intentionally not shown globally.
    """
    easter = _easter_sunday(year)
    return {
        date(year, 1, 1): "Neujahr",
        date(year, 1, 6): "Heilige Drei Könige",
        easter - timedelta(days=2): "Karfreitag",
        easter + timedelta(days=1): "Ostermontag",
        date(year, 5, 1): "Tag der Arbeit",
        easter + timedelta(days=39): "Christi Himmelfahrt",
        easter + timedelta(days=50): "Pfingstmontag",
        easter + timedelta(days=60): "Fronleichnam",
        date(year, 10, 3): "Tag der Deutschen Einheit",
        date(year, 11, 1): "Allerheiligen",
        date(year, 12, 25): "1. Weihnachtstag",
        date(year, 12, 26): "2. Weihnachtstag",
    }


def _easter_sunday(year: int) -> date:
    """Calculate Gregorian Easter Sunday with the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)
