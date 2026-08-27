from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.timezones import format_berlin_time
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.fleet_refresh import FleetRefreshService
from app.services.fleet_refresh_settings import (
    FleetRefreshRuntimeSettings,
    FleetRefreshSettingsError,
    FleetRefreshSettingsService,
)


def test_refresh_settings_store_berlin_schedule():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(user)
        db.commit()

        settings = FleetRefreshSettingsService(db=db).configure(
            actor=user,
            site_status_max_age_minutes=15,
            official_version_max_age_hours=24,
            max_parallel_site_checks=5,
            max_parallel_direct_updates=5,
            auto_refresh_enabled=True,
            auto_refresh_interval_hours=48,
            auto_refresh_time="02:30",
        )
        db.commit()

        assert settings.auto_refresh_enabled is True
        assert FleetRefreshSettingsService(db=db).get_runtime_settings().auto_refresh_time == "02:30"
        assert FleetRefreshSettingsService(db=db).get_runtime_settings().auto_refresh_interval_hours == 48


def test_refresh_schedule_requires_whole_days():
    with pytest.raises(FleetRefreshSettingsError, match="whole number of days"):
        FleetRefreshSettingsService._validate(
            site_status_max_age_minutes=15,
            official_version_max_age_hours=24,
            max_parallel_site_checks=5,
            max_parallel_direct_updates=5,
            auto_refresh_interval_hours=25,
            auto_refresh_time="03:00",
        )


def test_automatic_schedule_uses_berlin_calendar_days():
    class EmptyDatabase:
        def scalar(self, *_args, **_kwargs):
            return None

    settings = FleetRefreshRuntimeSettings(auto_refresh_time="03:00")
    assert FleetRefreshService._is_scheduled_run_due(
        db=EmptyDatabase(),
        runtime_settings=settings,
        now=datetime(2026, 8, 27, 1, 0, tzinfo=UTC),
    ) is True
    assert FleetRefreshService._is_scheduled_run_due(
        db=EmptyDatabase(),
        runtime_settings=settings,
        now=datetime(2026, 8, 27, 0, 59, tzinfo=UTC),
    ) is False


def test_berlin_time_format_handles_summer_winter_and_naive_database_values():
    assert format_berlin_time(datetime(2026, 8, 27, 12, 0, tzinfo=UTC)) == "27.08.2026 14:00:00 CEST"
    assert format_berlin_time(datetime(2026, 1, 15, 12, 0)) == "15.01.2026 13:00:00 CET"
