from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.timezones import format_berlin_time
from app.db.base import Base
from app.models.fleet_refresh_run import FleetRefreshRun, FleetRefreshRunStatus
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


def test_automatic_schedule_keeps_its_fixed_time_after_a_manual_refresh():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        scheduled = FleetRefreshRun(
            mode=FleetRefreshService.MODE_NORMAL,
            status=FleetRefreshRunStatus.succeeded.value,
            requested_by=FleetRefreshService.LEGACY_SCHEDULED_REQUESTED_BY,
            started_at=datetime(2026, 8, 27, 0, 0, tzinfo=UTC),
            completed_at=datetime(2026, 8, 27, 0, 5, tzinfo=UTC),
            result_json={},
        )
        manual = FleetRefreshRun(
            mode=FleetRefreshService.MODE_NORMAL,
            status=FleetRefreshRunStatus.succeeded.value,
            requested_by="operator",
            started_at=datetime(2026, 8, 28, 21, 30, tzinfo=UTC),
            completed_at=datetime(2026, 8, 28, 21, 35, tzinfo=UTC),
            result_json={},
        )
        db.add_all((scheduled, manual))
        db.commit()

        assert FleetRefreshService._is_scheduled_run_due(
            db=db,
            runtime_settings=FleetRefreshRuntimeSettings(auto_refresh_time="00:00"),
            now=datetime(2026, 8, 28, 22, 5, tzinfo=UTC),
        ) is True


def test_berlin_time_format_handles_summer_winter_and_naive_database_values():
    assert format_berlin_time(datetime(2026, 8, 27, 12, 0, tzinfo=UTC)) == "27.08.2026 14:00:00 CEST"
    assert format_berlin_time(datetime(2026, 1, 15, 12, 0)) == "15.01.2026 13:00:00 CET"


def test_cancel_queued_fleet_refresh_finishes_without_worker_starting():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="operator", password_hash="hashed", role="admin")
        run = FleetRefreshRun(
            mode=FleetRefreshService.MODE_NORMAL,
            status=FleetRefreshRunStatus.queued.value,
            requested_by=user.username,
            result_json=FleetRefreshService._initial_result(FleetRefreshService.MODE_NORMAL),
        )
        db.add_all((user, run))
        db.commit()

        stopped_run, stopped = FleetRefreshService(db=db).cancel_run(actor=user, run_id=run.id)
        db.commit()

        assert stopped is True
        assert stopped_run.status == FleetRefreshRunStatus.cancelled.value
        assert stopped_run.completed_at is not None
        assert stopped_run.result_json["cancellation"]["requested_by"] == "operator"


def test_cancel_running_fleet_refresh_keeps_it_exclusive_until_worker_stops():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="operator", password_hash="hashed", role="admin")
        run = FleetRefreshRun(
            mode=FleetRefreshService.MODE_NORMAL,
            status=FleetRefreshRunStatus.running.value,
            requested_by=user.username,
            result_json=FleetRefreshService._initial_result(FleetRefreshService.MODE_NORMAL),
        )
        db.add_all((user, run))
        db.commit()

        service = FleetRefreshService(db=db)
        stopped_run, stopped = service.cancel_run(actor=user, run_id=run.id)
        db.commit()

        assert stopped is True
        assert stopped_run.status == FleetRefreshRunStatus.cancelling.value
        existing_run, created = service.create_run(actor=user, mode=FleetRefreshService.MODE_FULL)

        assert created is False
        assert existing_run.id == run.id
