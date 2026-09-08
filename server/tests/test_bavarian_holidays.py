from datetime import date

from app.services.bavarian_holidays import bavarian_public_holidays


def test_bavarian_public_holidays_include_fixed_and_movable_statewide_days():
    holidays = bavarian_public_holidays(2026)

    assert holidays[date(2026, 1, 6)] == "Heilige Drei Könige"
    assert holidays[date(2026, 4, 3)] == "Karfreitag"
    assert holidays[date(2026, 5, 14)] == "Christi Himmelfahrt"
    assert holidays[date(2026, 6, 4)] == "Fronleichnam"
    assert holidays[date(2026, 12, 26)] == "2. Weihnachtstag"


def test_bavarian_public_holidays_exclude_location_dependent_days():
    holidays = bavarian_public_holidays(2026)

    assert date(2026, 8, 8) not in holidays
    assert date(2026, 8, 15) not in holidays
