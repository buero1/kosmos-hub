from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.services.fleet_refresh as fleet_refresh_module
from app.api.routes.web import _direct_update_batch_status_payload, _fleet_refresh_status_payload
from app.core.timezones import format_berlin_time
from app.db.base import Base
from app.models.fleet_refresh_run import FleetRefreshRun, FleetRefreshRunStatus, FleetRefreshSiteResult
from app.models.hub_user import HubUser
from app.models.site import Site, SiteStatus
from app.services.fleet_refresh import FleetRefreshService
from app.services.fleet_refresh_settings import (
    FleetRefreshRuntimeSettings,
    FleetRefreshSettingsError,
    FleetRefreshSettingsService,
)

def test_fresh_update_mode_is_valid_for_background_runs():
    FleetRefreshService._validate_mode(FleetRefreshService.MODE_FRESH_UPDATES)



def test_refresh_status_payload_exposes_live_phase_and_site_progress():
    run = FleetRefreshRun(
        id=42,
        mode=FleetRefreshService.MODE_NORMAL,
        status=FleetRefreshRunStatus.running.value,
        requested_by="operator",
        result_json={
            "scope": {"label": "4 selected site(s)"},
            "sites": {"completed": 2, "total": 4, "refreshed": 2, "cached": 0},
            "phase": {"key": "site-checks", "label": "Checking website status", "completed": 2, "total": 4},
            "last_site": "example.test",
        },
    )

    payload = _fleet_refresh_status_payload(run)

    assert payload["id"] == 42
    assert payload["status"] == "running"
    assert payload["result"]["scope"]["label"] == "4 selected site(s)"
    assert payload["result"]["sites"] == {"completed": 2, "total": 4, "refreshed": 2, "cached": 0}
    assert payload["result"]["phase"]["completed"] == 2
    assert payload["result"]["last_site"] == "example.test"


def test_direct_update_batch_status_payload_exposes_compact_live_rows():
    runs = [
        SimpleNamespace(
            id=18,
            status="succeeded",
            error_message=None,
            site=SimpleNamespace(id=8, domain="first.example"),
            result_json={
                "batch_position": 2,
                "update_kind": "plugin",
                "update_name": "Elementor",
                "current_version": "3.0.0",
                "target_version": "3.1.0",
                "stage": "complete",
            },
        ),
        SimpleNamespace(
            id=17,
            status="running",
            error_message=None,
            site=SimpleNamespace(id=7, domain="second.example"),
            result_json={
                "batch_position": 1,
                "update_kind": "theme",
                "update_name": "Hello Elementor",
                "current_version": "3.4.0",
                "target_version": "3.4.1",
                "stage": "processing",
                "stage_message": "Updating the selected version.",
            },
        ),
        SimpleNamespace(
            id=19,
            status="failed",
            error_message="The site did not confirm the update.",
            site=SimpleNamespace(id=9, domain="third.example"),
            result_json={"batch_position": 3, "update_name": "JetEngine"},
        ),
        SimpleNamespace(
            id=20,
            status="skipped",
            error_message=None,
            site=SimpleNamespace(id=10, domain="fourth.example"),
            result_json={"batch_position": 4, "plugin_name": "JetFormBuilder"},
        ),
    ]

    payload = _direct_update_batch_status_payload("a" * 32, runs)

    assert payload["batch_id"] == "a" * 32
    assert payload["total"] == 4
    assert payload["completed"] == 3
    assert payload["succeeded"] == 1
    assert payload["failed"] == 1
    assert payload["skipped"] == 1
    assert [row["id"] for row in payload["runs"]] == [17, 18, 19, 20]
    assert payload["runs"][0]["stage"] == "processing"
    assert payload["runs"][2]["error_message"] == "The site did not confirm the update."
    assert payload["runs"][3]["update_name"] == "JetFormBuilder"


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


def test_automatic_refresh_enables_crocoblock_provider_activation(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(fleet_refresh_module, "SessionLocal", sessionmaker(bind=engine))

    with Session(engine) as db:
        user = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(user)
        db.commit()
        FleetRefreshSettingsService(db=db).configure(
            actor=user,
            site_status_max_age_minutes=15,
            official_version_max_age_hours=24,
            max_parallel_site_checks=5,
            max_parallel_direct_updates=5,
            auto_refresh_enabled=True,
            auto_refresh_interval_hours=24,
            auto_refresh_time="00:00",
        )
        db.commit()

    run_id = FleetRefreshService.queue_scheduled_run()

    with Session(engine) as db:
        run = db.get(FleetRefreshRun, run_id)
        assert run is not None
        assert run.allow_provider_activation is True


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


def test_selected_refresh_scope_is_persisted_for_the_background_worker():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(user)
        db.commit()

        run, created = FleetRefreshService(db=db).create_run(
            actor=user,
            mode=FleetRefreshService.MODE_NORMAL,
            site_ids={19, 7, 11},
        )

        assert created is True
        assert run.allow_provider_activation is False
        assert run.result_json["scope"] == {
            "kind": "selected",
            "site_ids": [7, 11, 19],
            "count": 3,
            "label": "3 selected site(s)",
        }
        assert FleetRefreshService._target_site_ids(run.result_json) == {7, 11, 19}
        assert FleetRefreshService._target_site_ids(FleetRefreshService._initial_result(FleetRefreshService.MODE_NORMAL)) is None


def test_refresh_site_result_keeps_the_website_and_jet_license_outcomes(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(fleet_refresh_module, "SessionLocal", sessionmaker(bind=engine))

    with Session(engine) as db:
        site = Site(
            uuid="refresh-result-site",
            domain="example.test",
            home_url="https://example.test",
            site_url="https://example.test",
            status=SiteStatus.verified.value,
        )
        run = FleetRefreshRun(
            mode=FleetRefreshService.MODE_NORMAL,
            status=FleetRefreshRunStatus.running.value,
            requested_by="operator",
            result_json=FleetRefreshService._initial_result(FleetRefreshService.MODE_NORMAL),
        )
        db.add_all((site, run))
        db.commit()

        run_id = run.id
        site_id = site.id

    FleetRefreshService._store_site_result(
        run_id=run_id,
        outcome={
            "site_id": site_id,
            "domain": "example.test",
            "state": "refreshed",
            "updates": "refreshed",
            "backups": "refreshed",
            "users": "unsupported",
            "detail": "Website evidence was refreshed.",
            "errors": [],
        },
    )
    FleetRefreshService._store_jet_result(
        run_id=run_id,
        outcome={
            "site_id": site_id,
            "status": "activation-verified",
            "detail": "The stored Crocoblock license was not active and was activated for this website.",
            "license_was_already_active": False,
            "update_package_ready": True,
            "plugins": [{"plugin_file": "jet-engine/jet-engine.php", "name": "JetEngine"}],
            "provider_versions": [{"plugin_file": "jet-engine/jet-engine.php", "version": "3.8.14.3"}],
        },
    )

    with Session(engine) as db:
        record = db.scalar(select(FleetRefreshSiteResult))
        assert record is not None
        assert record.state_status == "refreshed"
        assert record.updates_status == "refreshed"
        assert record.users_status == "unsupported"
        assert record.jet_status == "activation-verified"
        assert record.result_json["jet"] == {
            "status": "activation-verified",
            "detail": "The stored Crocoblock license was not active and was activated for this website.",
            "license_was_already_active": False,
            "update_package_ready": True,
            "plugins": [{"plugin_file": "jet-engine/jet-engine.php", "name": "JetEngine"}],
            "provider_versions": [{"plugin_file": "jet-engine/jet-engine.php", "version": "3.8.14.3"}],
        }


def test_active_refresh_status_only_returns_running_work():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        global_run = FleetRefreshRun(
            mode=FleetRefreshService.MODE_NORMAL,
            status=FleetRefreshRunStatus.succeeded.value,
            requested_by="operator",
            result_json=FleetRefreshService._initial_result(FleetRefreshService.MODE_NORMAL),
        )
        selected_run = FleetRefreshRun(
            mode=FleetRefreshService.MODE_NORMAL,
            status=FleetRefreshRunStatus.succeeded.value,
            requested_by="operator",
            result_json=FleetRefreshService._initial_result(
                FleetRefreshService.MODE_NORMAL,
                target_site_ids={7, 11},
            ),
        )
        db.add_all((global_run, selected_run))
        db.flush()

        service = FleetRefreshService(db=db)
        assert service.get_active_run() is None

        selected_run.status = FleetRefreshRunStatus.running.value
        db.flush()
        assert service.get_active_run().id == selected_run.id
