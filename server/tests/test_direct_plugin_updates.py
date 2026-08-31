from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.maintenance_run import MaintenanceRun, MaintenanceRunStatus, MaintenanceRunStep, MaintenanceRunStepStatus
from app.models.site import Site, SiteStatus
from app.models.site_backup_snapshot import SiteBackupSnapshot
from app.models.site_capability import SiteCapability
from app.models.site_connection import SiteConnection
from app.models.site_snapshot import SiteSnapshot
from app.models.site_update_snapshot import SiteUpdateSnapshot
from app.models.site_user_snapshot import SiteUserSnapshot
from app.models.update_plan import UpdatePlan, UpdatePlanItem
from app.services.fleet_inventory import FleetInventoryItem, FleetInventoryService, UpdateWorkbenchEntry
from app.services.fleet_refresh import FleetRefreshService
from app.services.fleet_refresh_settings import FleetRefreshRuntimeSettings
from app.services.maintenance_runs import MaintenanceRunService
from app.services.official_plugin_versions import OfficialPluginVersionService
from app.services.provider_credentials import ProviderCredentialService
from app.services.crocoblock_license import CrocoblockLicenseService
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService
from app.services.site_updates import SiteUpdateService


def plugin_entry(**overrides):
    values = {
        "kind": "plugin",
        "name": "Smush",
        "identifier": "wp-smushit/wp-smush.php",
        "is_active": True,
        "current_version": "3.22.1",
        "target_version": "3.22.2",
        "execution_ready": True,
        "execution_note": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_direct_updates_accept_active_plugin_with_exact_versions():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry()) is None


def test_direct_updates_default_to_five_parallel_customer_sites():
    assert FleetRefreshRuntimeSettings().max_parallel_direct_updates == 5


def test_direct_updates_accept_inactive_plugins():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry(is_active=False)) is None


def test_large_direct_plugin_batches_require_the_update_confirmation_word():
    entries = [plugin_entry() for _ in range(101)]

    assert MaintenanceRunService._large_direct_plugin_batch_confirmation_error(entries[:100], "") is None
    assert "Type update" in MaintenanceRunService._large_direct_plugin_batch_confirmation_error(entries, "")
    assert MaintenanceRunService._large_direct_plugin_batch_confirmation_error(entries, "update") is None
    assert MaintenanceRunService._large_direct_plugin_batch_confirmation_error(entries, "UPDATE") is None


def test_direct_update_details_preserve_an_inactive_selection():
    details = MaintenanceRunService._direct_update_details(
        SimpleNamespace(
            result_json={
                "plugin_file": "wp-smushit/wp-smush.php",
                "plugin_name": "Smush",
                "current_version": "3.22.1",
                "target_version": "3.22.2",
                "expected_active": False,
            }
        )
    )

    assert details is not None
    assert details["expected_active"] is False


def test_direct_update_details_accept_theme_and_wordpress_core_scopes():
    theme = MaintenanceRunService._direct_update_details(
        SimpleNamespace(
            result_json={
                "update_kind": "theme",
                "update_identifier": "hello-elementor",
                "update_name": "Hello Elementor",
                "current_version": "3.4.1",
                "target_version": "3.4.2",
            }
        )
    )
    core = MaintenanceRunService._direct_update_details(
        SimpleNamespace(
            result_json={
                "update_kind": "wordpress",
                "update_identifier": "wordpress-core",
                "update_name": "WordPress core",
                "current_version": "6.8.1",
                "target_version": "6.8.2",
            }
        )
    )

    assert theme is not None
    assert theme["update_identifier"] == "hello-elementor"
    assert core is not None
    assert core["update_identifier"] == "wordpress-core"


def test_confirmed_plugin_update_reconciles_only_the_matching_inventory_entry():
    plugins, changed = SiteInventoryService._replace_confirmed_component(
        [
            {"plugin_file": "akismet/akismet.php", "version": "5.4.1", "active": True},
            {"plugin_file": "hello-dolly/hello.php", "version": "1.7.2", "active": False},
        ],
        identifier_field="plugin_file",
        identifier="akismet/akismet.php",
        installed_version="5.4.2",
        active=True,
    )

    assert changed is True
    assert plugins == [
        {"plugin_file": "akismet/akismet.php", "version": "5.4.2", "active": True},
        {"plugin_file": "hello-dolly/hello.php", "version": "1.7.2", "active": False},
    ]


def test_confirmed_plugin_update_removes_only_its_stored_offer():
    updates, changed = SiteUpdateService._without_confirmed_update(
        [
            {"plugin_file": "akismet/akismet.php", "current_version": "5.4.1", "new_version": "5.4.2"},
            {"plugin_file": "hello-dolly/hello.php", "current_version": "1.7.2", "new_version": "1.7.3"},
        ],
        identifier_field="plugin_file",
        identifier="akismet/akismet.php",
    )

    assert changed is True
    assert updates == [
        {"plugin_file": "hello-dolly/hello.php", "current_version": "1.7.2", "new_version": "1.7.3"}
    ]


def test_direct_update_preflight_adopts_a_newer_target_version_without_losing_the_original_selection():
    run = SimpleNamespace(result_json={"target_version": "0.3.55"})
    details = {
        "update_name": "Kosmos Bridge",
        "update_kind": "plugin",
        "update_identifier": "kosmos-bridge/kosmos-bridge.php",
        "current_version": "0.3.52",
        "target_version": "0.3.55",
        "expected_active": True,
    }
    service = object.__new__(MaintenanceRunService)
    service._current_plugin_update_entry = lambda _run, _details: plugin_entry(
        name="Kosmos Bridge",
        identifier="kosmos-bridge/kosmos-bridge.php",
        current_version="0.3.52",
        target_version="0.3.56",
    )

    error, note = service._direct_plugin_update_preflight(run, details)

    assert error is None
    assert "0.3.55 to 0.3.56" in note
    assert details["target_version"] == "0.3.56"
    assert run.result_json["selected_target_version"] == "0.3.55"
    assert run.result_json["target_version"] == "0.3.56"


def test_direct_update_preflight_adopts_the_fresh_installed_version():
    run = SimpleNamespace(result_json={})
    details = {
        "update_name": "Kosmos Bridge",
        "update_kind": "plugin",
        "update_identifier": "kosmos-bridge/kosmos-bridge.php",
        "current_version": "0.3.52",
        "target_version": "0.3.55",
        "expected_active": True,
    }
    service = object.__new__(MaintenanceRunService)
    service._current_plugin_update_entry = lambda _run, _details: plugin_entry(
        name="Kosmos Bridge",
        identifier="kosmos-bridge/kosmos-bridge.php",
        current_version="0.3.56",
        target_version="0.3.57",
    )

    error, note = service._direct_plugin_update_preflight(run, details)

    assert error is None
    assert "installed version changed from 0.3.52 to 0.3.56" in note
    assert details["current_version"] == "0.3.56"
    assert details["target_version"] == "0.3.57"
    assert run.result_json["selected_current_version"] == "0.3.52"
    assert run.result_json["current_version"] == "0.3.56"


def test_final_bridge_preflight_marks_a_newer_installed_version_as_already_updated():
    details = {
        "update_name": "Kosmos Bridge",
        "update_kind": "plugin",
        "update_identifier": "kosmos-bridge/kosmos-bridge.php",
        "target_version": "0.3.58",
    }
    error = SiteMcpProxyError(
        "KOSMOS_BRIDGE_UPDATE_VERSION_MISMATCH",
        "The approved plugin is at version 0.3.59, not the approved version 0.3.58.",
        status_code=409,
        details={"installed_version": "0.3.59", "active": True},
    )

    resolution = MaintenanceRunService._bridge_update_preflight_resolution(details, error)

    assert resolution is not None
    assert resolution["outcome"] == "succeeded"
    assert resolution["stage"] == "already-updated"
    assert resolution["installed_version"] == "0.3.59"


def test_final_bridge_preflight_adopts_a_new_target_version_for_one_safe_retry():
    run = SimpleNamespace(result_json={})
    details = {
        "update_name": "Kosmos Bridge",
        "update_kind": "plugin",
        "update_identifier": "kosmos-bridge/kosmos-bridge.php",
        "current_version": "0.3.57",
        "target_version": "0.3.58",
    }
    error = SiteMcpProxyError(
        "KOSMOS_BRIDGE_UPDATE_OFFER_CHANGED",
        "The approved plugin update offer is no longer available.",
        status_code=409,
        details={
            "installed_version": "0.3.57",
            "active": True,
            "offered_version": "0.3.59",
            "package_available": True,
        },
    )

    resolution = MaintenanceRunService._bridge_update_preflight_resolution(details, error)

    assert resolution is not None
    assert resolution["action"] == "retry"
    MaintenanceRunService._adopt_bridge_update_preflight_values(run, details, resolution)
    assert details["target_version"] == "0.3.59"
    assert run.result_json["selected_target_version"] == "0.3.58"
    assert run.result_json["bridge_final_preflight_retries"] == 1


def test_final_bridge_preflight_skips_a_missing_package_without_marking_a_failure():
    details = {
        "update_name": "Example Plugin",
        "update_kind": "plugin",
        "update_identifier": "example/example.php",
        "target_version": "2.0.0",
    }
    error = SiteMcpProxyError(
        "KOSMOS_BRIDGE_UPDATE_OFFER_CHANGED",
        "The approved plugin update offer is no longer available.",
        status_code=409,
        details={"installed_version": "1.0.0", "offered_version": "", "package_available": False},
    )

    resolution = MaintenanceRunService._bridge_update_preflight_resolution(details, error)

    assert resolution is not None
    assert resolution["outcome"] == "skipped"
    assert resolution["stage"] == "update-not-available"


def test_final_bridge_preflight_is_enabled_only_after_the_bridge_release_is_installed():
    assert MaintenanceRunService._bridge_enforces_final_update_preflight(SimpleNamespace(bridge_version="0.3.58")) is False
    assert MaintenanceRunService._bridge_enforces_final_update_preflight(SimpleNamespace(bridge_version="0.3.59")) is True


def test_plugin_update_error_reconciles_a_client_error_after_wordpress_confirms_target_and_activation():
    run = SimpleNamespace(site_id=42, result_json={})
    details = {
        "update_name": "JetReviews For Elementor",
        "update_kind": "plugin",
        "update_identifier": "jet-reviews/jet-reviews.php",
        "current_version": "2.3.6",
        "target_version": "3.1.1",
        "expected_active": True,
    }
    service = object.__new__(MaintenanceRunService)
    service.db = SimpleNamespace(commit=lambda: None)
    service._start_plugin_update_step = lambda *_args: None
    service.proxy = SimpleNamespace(
        execute_readonly_ability=lambda site_id, ability_name, payload, **_kwargs: {
            "result": {
                "plugins": [
                    {
                        "plugin_file": details["update_identifier"],
                        "version": details["target_version"],
                        "active": True,
                    }
                ]
            }
        }
    )

    result, detail = service._reconcile_plugin_after_failed_update_request(
        run,
        details,
        None,
        SiteMcpProxyError("REST_NO_ROUTE", "No route was found for the request.", status_code=404),
    )

    assert detail == ""
    assert result == {
        "updated": True,
        "plugin_file": "jet-reviews/jet-reviews.php",
        "previous_version": "2.3.6",
        "installed_version": "3.1.1",
        "active": True,
        "reconciled_after_update_error": True,
        "post_update_error": "No route was found for the request.",
    }
    assert run.result_json["post_update_reconciliation"] == {
        "attempted": True,
        "original_error": "No route was found for the request.",
        "original_error_code": "REST_NO_ROUTE",
        "original_error_status": 404,
        "plugin_file": "jet-reviews/jet-reviews.php",
        "target_version": "3.1.1",
        "expected_active": True,
        "confirmed": True,
        "installed_version": "3.1.1",
        "installed_active": True,
        "activation_recovery_attempted": False,
        "activated": False,
    }


def test_plugin_update_error_remains_failed_when_wordpress_cannot_confirm_the_target_version():
    run = SimpleNamespace(site_id=42, result_json={})
    details = {
        "update_name": "JetReviews For Elementor",
        "update_kind": "plugin",
        "update_identifier": "jet-reviews/jet-reviews.php",
        "current_version": "2.3.6",
        "target_version": "3.1.1",
        "expected_active": True,
    }
    service = object.__new__(MaintenanceRunService)
    service.db = SimpleNamespace(commit=lambda: None)
    service._start_plugin_update_step = lambda *_args: None
    service.proxy = SimpleNamespace(
        execute_readonly_ability=lambda *_args, **_kwargs: {
            "result": {
                "plugins": [
                    {
                        "plugin_file": details["update_identifier"],
                        "version": "2.3.6",
                        "active": True,
                    }
                ]
            }
        }
    )

    result, detail = service._reconcile_plugin_after_failed_update_request(
        run,
        details,
        None,
        SiteMcpProxyError("REMOTE_ERROR", "HTTP Error 500: Internal Server Error", status_code=500),
    )

    assert result is None
    assert detail == "WordPress did not confirm the planned target version."
    assert run.result_json["post_update_reconciliation"]["confirmed"] is False
    assert run.result_json["post_update_reconciliation"]["installed_version"] == "2.3.6"


def test_plugin_update_error_restores_an_active_plugin_only_after_target_version_confirmation():
    run = SimpleNamespace(site_id=42, result_json={})
    details = {
        "update_name": "JetReviews For Elementor",
        "update_kind": "plugin",
        "update_identifier": "jet-reviews/jet-reviews.php",
        "current_version": "2.3.6",
        "target_version": "3.1.1",
        "expected_active": True,
    }
    service = object.__new__(MaintenanceRunService)
    service.db = SimpleNamespace(commit=lambda: None)
    service._start_plugin_update_step = lambda *_args: None
    service.proxy = SimpleNamespace(
        execute_readonly_ability=lambda *_args, **_kwargs: {
            "result": {
                "plugins": [
                    {
                        "plugin_file": details["update_identifier"],
                        "version": details["target_version"],
                        "active": False,
                    }
                ]
            }
        },
        execute_ability=lambda _site_id, _ability_name, payload, **_kwargs: {
            "result": {
                "activated": True,
                "plugin_file": payload["plugin_file"],
                "installed_version": payload["expected_installed_version"],
                "active": True,
            }
        },
    )

    result, detail = service._reconcile_plugin_after_failed_update_request(
        run,
        details,
        None,
        SiteMcpProxyError("REMOTE_ERROR", "The update callback failed.", status_code=500),
    )

    assert detail == ""
    assert result is not None
    assert result["active"] is True
    assert run.result_json["post_update_reconciliation"]["activation_recovery_attempted"] is True
    assert run.result_json["post_update_reconciliation"]["activated"] is True


def test_plugin_update_error_reconciles_an_inactive_plugin_when_wordpress_preserved_its_state():
    service = object.__new__(MaintenanceRunService)
    service.db = SimpleNamespace(commit=lambda: None)
    service._start_plugin_update_step = lambda *_args: None
    details = {
        "update_name": "Example Plugin",
        "update_kind": "plugin",
        "update_identifier": "example/example.php",
        "current_version": "1.0.0",
        "target_version": "1.1.0",
        "expected_active": False,
    }
    service.proxy = SimpleNamespace(
        execute_readonly_ability=lambda *_args, **_kwargs: {
            "result": {
                "plugins": [
                    {
                        "plugin_file": details["update_identifier"],
                        "version": "1.1.0",
                        "active": False,
                    }
                ]
            }
        }
    )

    result, detail = service._reconcile_plugin_after_failed_update_request(
        SimpleNamespace(site_id=42, result_json={}),
        details,
        None,
        SiteMcpProxyError("UPDATE_OFFER_CHANGED", "The offer changed.", status_code=409),
    )

    assert detail == ""
    assert result is not None
    assert result["installed_version"] == "1.1.0"
    assert result["active"] is False


def test_failed_theme_update_is_not_reconciled_as_a_plugin():
    service = object.__new__(MaintenanceRunService)
    service.db = SimpleNamespace(commit=lambda: None)
    service._start_plugin_update_step = lambda *_args: (_ for _ in ()).throw(AssertionError())
    service.proxy = SimpleNamespace(execute_readonly_ability=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    details = {
        "update_name": "Example Theme",
        "update_kind": "theme",
        "update_identifier": "example-theme",
        "current_version": "1.0.0",
        "target_version": "1.1.0",
        "expected_active": None,
    }

    result, detail = service._reconcile_plugin_after_failed_update_request(
        SimpleNamespace(site_id=42, result_json={}),
        details,
        None,
        SiteMcpProxyError("UPDATE_OFFER_CHANGED", "The offer changed.", status_code=409),
    )

    assert result is None
    assert detail == ""


def test_direct_update_failure_streak_stops_after_five_consecutive_failures():
    failed = lambda position: SimpleNamespace(
        id=position,
        status="failed",
        result_json={"batch_position": position},
    )
    succeeded = lambda position: SimpleNamespace(
        id=position,
        status="succeeded",
        result_json={"batch_position": position},
    )
    running = lambda position: SimpleNamespace(
        id=position,
        status="running",
        result_json={"batch_position": position},
    )

    assert MaintenanceRunService._has_direct_update_failure_streak([failed(1), failed(2), failed(3), failed(4)]) is False
    assert MaintenanceRunService._has_direct_update_failure_streak([failed(1), succeeded(2), failed(3), failed(4), failed(5), failed(6)]) is False
    assert MaintenanceRunService._has_direct_update_failure_streak([failed(1), failed(2), failed(3), failed(4), failed(5)]) is True
    assert MaintenanceRunService._has_direct_update_failure_streak([failed(1), running(2), failed(3), failed(4), failed(5), failed(6)]) is False


def test_direct_update_batch_skips_remaining_runs_only_after_the_failure_limit():
    batch_id = "a" * 32
    failed_runs = [
        SimpleNamespace(id=position, status="failed", result_json={"batch_position": position})
        for position in range(1, 6)
    ]
    service = object.__new__(MaintenanceRunService)
    service._direct_update_batch_runs = lambda _batch_id: failed_runs
    skipped_calls = []
    service._skip_queued_maintenance_runs = lambda *args, **kwargs: (skipped_calls.append((args, kwargs)) or 3)

    skipped = service.stop_direct_update_batches_after_failure_streak({batch_id})

    assert skipped == 3
    assert skipped_calls[0][0] == (batch_id,)
    assert skipped_calls[0][1]["kind"] == MaintenanceRunService.PLUGIN_UPDATE_KIND
    assert "5 consecutive" in skipped_calls[0][1]["message"]


def test_direct_update_batch_cancellation_stops_queued_updates_but_keeps_processing_update_running():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    batch_id = "a" * 32
    now = datetime.now(UTC)

    with Session(engine) as db:
        site = Site(
            uuid="12345678-1234-1234-1234-123456789012",
            domain="example.test",
            home_url="https://example.test",
            site_url="https://example.test",
            status=SiteStatus.verified.value,
        )
        processing = MaintenanceRun(
            site=site,
            kind=MaintenanceRunService.PLUGIN_UPDATE_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by="operator",
            started_at=now,
            result_json={"batch_id": batch_id, "batch_position": 1, "stage": "processing"},
        )
        queued = MaintenanceRun(
            site=site,
            kind=MaintenanceRunService.PLUGIN_UPDATE_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by="operator",
            started_at=now,
            result_json={"batch_id": batch_id, "batch_position": 2, "stage": "queued"},
        )
        queued.steps.append(
            MaintenanceRunStep(
                step_key="preflight",
                status=MaintenanceRunStepStatus.waiting.value,
                started_at=now,
                result_json={},
            )
        )
        db.add_all((processing, queued))
        db.commit()

        outcome = MaintenanceRunService(db=db, cipher=None).cancel_direct_update_batch(batch_id=batch_id, actor="operator")
        db.commit()

        assert outcome.cancelled_queued_runs == 1
        assert outcome.processing_runs == 1
        assert processing.status == MaintenanceRunStatus.running.value
        assert processing.result_json["cancellation"]["requested_by"] == "operator"
        assert queued.status == MaintenanceRunStatus.skipped.value
        assert queued.result_json["stage"] == "cancelled"
        assert queued.steps[0].status == MaintenanceRunStepStatus.skipped.value


def test_complete_site_update_starts_with_a_single_live_parent_run_and_can_be_cancelled():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        site = Site(
            uuid="f23dcd24-56ef-78ab-90cd-12ef34ab56cd",
            domain="complete-workflow.example",
            home_url="https://complete-workflow.example",
            site_url="https://complete-workflow.example",
            status=SiteStatus.verified.value,
        )
        db.add(site)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=None)
        outcome = service.start_complete_site_update(site_id=site.id, actor="operator")

        assert outcome.result == "started"
        assert outcome.run.kind == MaintenanceRunService.COMPLETE_SITE_UPDATE_KIND
        assert outcome.run.result_json["stage"] == "queued"
        assert outcome.run.result_json["max_waves"] == 3
        assert [step.step_key for step in outcome.run.steps] == ["workflow"]
        assert service.next_complete_site_update_run_ids() == [outcome.run.id]

        assert service.cancel_complete_site_update(run_id=outcome.run.id, actor="operator") is True
        cancelled = service.get_complete_site_update_run(outcome.run.id)
        assert cancelled is not None
        assert cancelled.result_json["cancellation"]["requested_by"] == "operator"


def test_complete_site_update_runs_fresh_wordpress_theme_plugin_phases_until_stable(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def entry(kind, name, identifier, current, target):
        return SimpleNamespace(
            kind=kind,
            name=name,
            identifier=identifier,
            current_version=current,
            target_version=target,
            update_available=True,
        )

    phase_entries = {
        "wordpress": [entry("wordpress", "WordPress core", "wordpress-core", "6.8.8", "7.1")],
        "theme": [entry("theme", "Hello Elementor", "hello-elementor", "3.2.0", "3.2.1")],
        "plugin": [entry("plugin", "Elementor", "elementor/elementor.php", "3.30.0", "3.30.1")],
        "verification": [],
    }

    with Session(engine) as db:
        site = Site(
            uuid="c23dcd24-56ef-78ab-90cd-12ef34ab56cd",
            domain="workflow-phases.example",
            home_url="https://workflow-phases.example",
            site_url="https://workflow-phases.example",
            status=SiteStatus.verified.value,
        )
        db.add(site)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=None)
        outcome = service.start_complete_site_update(site_id=site.id, actor="operator")
        run = service.get_complete_site_update_run(outcome.run.id)
        assert run is not None

        fresh_phases = []
        child_runs = []

        def fresh_entries(_run, *, phase, wave):
            fresh_phases.append((phase, wave))
            return phase_entries[phase], None

        def create_child(_run, update, *, phase, wave):
            child = SimpleNamespace(id=len(child_runs) + 1, result_json={}, error_message=None)
            child_runs.append((phase, wave, update.name))
            return child

        def complete_child(child):
            child.result_json = {"stage_message": "Updated and verified."}
            return "succeeded"

        monkeypatch.setattr(service, "_fresh_complete_site_update_entries", fresh_entries)
        monkeypatch.setattr(service, "_complete_site_update_entries_by_readiness", lambda entries: (entries, []))
        monkeypatch.setattr(service, "_create_complete_site_update_child_run", create_child)
        monkeypatch.setattr(service, "_poll_plugin_update", complete_child)

        assert service.poll_complete_site_update_run(run.id) == "succeeded"

        completed = service.get_complete_site_update_run(run.id)
        assert completed is not None
        assert completed.status == MaintenanceRunStatus.succeeded.value
        assert completed.result_json["workflow_phase"] == "completed"
        assert fresh_phases == [("wordpress", 1), ("theme", 1), ("plugin", 1), ("verification", 1)]
        assert child_runs == [
            ("wordpress", 1, "WordPress core"),
            ("theme", 1, "Hello Elementor"),
            ("plugin", 1, "Elementor"),
        ]
        assert completed.result_json["successful_updates"] == 3
        assert any(event["status"] == "processing" for event in completed.result_json["events"])
        assert any(event["status"] == "succeeded" for event in completed.result_json["events"])


def test_complete_site_update_stops_one_site_after_an_admin_ajax_health_failure(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def entry(name, identifier):
        return SimpleNamespace(
            kind="plugin",
            name=name,
            identifier=identifier,
            current_version="1.0.0",
            target_version="1.1.0",
            update_available=True,
        )

    phase_entries = {
        "wordpress": [],
        "theme": [],
        "plugin": [
            entry("JetEngine", "jet-engine/jet-engine.php"),
            entry("JetReviews", "jet-reviews/jet-reviews.php"),
        ],
        "verification": [],
    }

    with Session(engine) as db:
        site = Site(
            uuid="d23dcd24-56ef-78ab-90cd-12ef34ab56cd",
            domain="workflow-health.example",
            home_url="https://workflow-health.example",
            site_url="https://workflow-health.example",
            status=SiteStatus.verified.value,
        )
        db.add(site)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=None)
        outcome = service.start_complete_site_update(site_id=site.id, actor="operator")
        run = service.get_complete_site_update_run(outcome.run.id)
        assert run is not None

        child_runs = []
        monkeypatch.setattr(service, "_fresh_complete_site_update_entries", lambda _run, *, phase, wave: (phase_entries[phase], None))
        monkeypatch.setattr(service, "_complete_site_update_entries_by_readiness", lambda entries: (entries, []))

        def create_child(_run, update, *, phase, wave):
            child = SimpleNamespace(id=len(child_runs) + 1, result_json={}, error_message=None)
            child_runs.append(update.name)
            return child

        def fail_admin_ajax_health(child):
            child.error_message = "JetEngine was updated, but the WordPress admin AJAX health check did not pass."
            child.result_json = {
                "stage_message": child.error_message,
                "post_update_health": {
                    "home_healthy": True,
                    "rest_healthy": True,
                    "admin_ajax_healthy": False,
                    "admin_ajax_status": 500,
                },
            }
            return "failed"

        monkeypatch.setattr(service, "_create_complete_site_update_child_run", create_child)
        monkeypatch.setattr(service, "_poll_plugin_update", fail_admin_ajax_health)

        assert service.poll_complete_site_update_run(run.id) == "failed"

        completed = service.get_complete_site_update_run(run.id)
        assert completed is not None
        assert completed.result_json["stage"] == "post-update-health-failed"
        assert "admin AJAX" in completed.result_json["stage_message"]
        assert child_runs == ["JetEngine"]


def test_live_plugin_preflight_marks_a_newer_installed_version_as_already_updated():
    details = {
        "update_name": "Kosmos Bridge",
        "target_version": "0.3.55",
    }

    resolution = MaintenanceRunService._direct_plugin_live_preflight_resolution(
        details,
        {"plugin_file": "kosmos-bridge/kosmos-bridge.php", "version": "0.3.56", "active": True},
        "Kosmos Bridge does not report both the installed and target version.",
    )

    assert resolution["outcome"] == "succeeded"
    assert resolution["stage"] == "already-updated"
    assert resolution["installed_version"] == "0.3.56"
    assert "No update was required" in resolution["message"]


def test_live_plugin_preflight_skips_an_unavailable_update_with_the_observed_version():
    details = {
        "update_name": "Kosmos Bridge",
        "target_version": "0.3.56",
    }

    resolution = MaintenanceRunService._direct_plugin_live_preflight_resolution(
        details,
        {"plugin_file": "kosmos-bridge/kosmos-bridge.php", "version": "0.3.52", "active": True},
        "Kosmos Bridge does not report both the installed and target version.",
    )

    assert resolution["outcome"] == "skipped"
    assert resolution["stage"] == "update-not-available"
    assert resolution["installed_version"] == "0.3.52"
    assert "cannot be performed" in resolution["message"]


def test_live_plugin_preflight_reads_installed_plugins_without_an_input_payload():
    class Proxy:
        def __init__(self):
            self.input_payload = "not-called"

        def execute_readonly_ability(self, _site_id, _ability_name, input_payload, *, timeout_seconds):
            self.input_payload = input_payload
            assert timeout_seconds == 45
            return {
                "result": {
                    "plugins": [
                        {
                            "plugin_file": "kosmos-bridge/kosmos-bridge.php",
                            "version": "0.3.56",
                            "active": True,
                        }
                    ]
                }
            }

    service = object.__new__(MaintenanceRunService)
    service.proxy = Proxy()
    service._start_plugin_update_step = lambda *_args: None
    captured = {}
    service._finish_direct_plugin_preflight_resolution = lambda _run, _step, _details, resolution: captured.update(resolution)
    outcome = service._reconcile_direct_plugin_preflight_mismatch(
        SimpleNamespace(site_id=17),
        {
            "update_kind": "plugin",
            "update_name": "Kosmos Bridge",
            "update_identifier": "kosmos-bridge/kosmos-bridge.php",
            "target_version": "0.3.55",
        },
        None,
        "Kosmos Bridge does not report both the installed and target version.",
    )

    assert service.proxy.input_payload is None
    assert outcome == "succeeded"
    assert captured["stage"] == "already-updated"


def test_direct_updates_accept_themes_and_wordpress_core_with_exact_versions():
    theme = plugin_entry(
        kind="theme",
        name="Hello Elementor",
        identifier="hello-elementor",
        is_active=None,
    )
    core = plugin_entry(
        kind="wordpress",
        name="WordPress core",
        identifier="wordpress-core",
        is_active=None,
    )

    assert MaintenanceRunService._direct_plugin_update_scope_error(theme) is None
    assert MaintenanceRunService._direct_plugin_update_scope_error(core) is None


def test_site_direct_updates_reject_a_selection_from_another_site():
    entries = [
        SimpleNamespace(site=SimpleNamespace(id=7)),
        SimpleNamespace(site=SimpleNamespace(id=8)),
    ]

    assert MaintenanceRunService._selection_scope_error(entries, expected_site_id=7) == (
        "Select updates from this customer site only."
    )


def test_update_workbench_requires_the_matching_bridge_ability_for_core_and_themes():
    captured_at = datetime.now(UTC)
    item = FleetInventoryItem(
        site=SimpleNamespace(id=17, domain="example.test"),
        snapshot=SimpleNamespace(captured_at=captured_at),
        update_snapshot=SimpleNamespace(
            captured_at=captured_at,
            core_updates_json=[{"current_version": "6.8.1", "new_version": "6.8.2"}],
            plugin_updates_json=[],
            theme_updates_json=[
                {
                    "stylesheet": "hello-elementor",
                    "name": "Hello Elementor",
                    "current_version": "3.4.1",
                    "new_version": "3.4.2",
                }
            ],
        ),
        plugins=(),
        ability_names=frozenset({"kosmos-bridge/update-wordpress-core"}),
    )

    service = object.__new__(FleetInventoryService)
    service._attach_official_plugin_versions = lambda entries: entries
    entries = service.build_update_workbench([item])
    by_kind = {entry.kind: entry for entry in entries}

    assert by_kind["wordpress"].direct_update_selectable is True
    assert by_kind["theme"].direct_update_selectable is False
    assert "0.3.48" in by_kind["theme"].review_note


def test_update_workbench_includes_plugins_without_available_updates():
    captured_at = datetime.now(UTC)
    item = FleetInventoryItem(
        site=SimpleNamespace(id=17, domain="example.test"),
        snapshot=SimpleNamespace(captured_at=captured_at),
        update_snapshot=SimpleNamespace(
            captured_at=captured_at,
            core_updates_json=[],
            plugin_updates_json=[
                {
                    "plugin_file": "akismet/akismet.php",
                    "name": "Akismet",
                    "current_version": "5.4.1",
                    "new_version": "5.4.2",
                    "execution_ready": True,
                }
            ],
            theme_updates_json=[],
        ),
        plugins=(
            {
                "plugin_file": "akismet/akismet.php",
                "name": "Akismet",
                "version": "5.4.1",
                "active": True,
            },
            {
                "plugin_file": "hello-dolly/hello.php",
                "name": "Hello Dolly",
                "version": "1.7.2",
                "active": False,
            },
        ),
    )

    service = object.__new__(FleetInventoryService)
    service._attach_official_plugin_versions = lambda entries: entries
    entries = service.build_update_workbench([item])
    by_plugin = {entry.identifier: entry for entry in entries}

    assert by_plugin["akismet/akismet.php"].direct_update_selectable is True
    assert by_plugin["hello-dolly/hello.php"].update_available is False
    assert by_plugin["hello-dolly/hello.php"].direct_update_selectable is False
    assert by_plugin["hello-dolly/hello.php"].review_note == "No update is currently available."


def test_update_workbench_prefers_the_fresh_installed_version_from_an_update_check():
    captured_at = datetime.now(UTC)
    item = FleetInventoryItem(
        site=SimpleNamespace(id=17, domain="example.test"),
        snapshot=SimpleNamespace(captured_at=captured_at),
        update_snapshot=SimpleNamespace(
            captured_at=captured_at,
            core_updates_json=[],
            plugin_updates_json=[
                {
                    "plugin_file": "kosmos-bridge/kosmos-bridge.php",
                    "name": "Kosmos Bridge",
                    "current_version": "0.3.56",
                    "new_version": "0.3.58",
                    "execution_ready": True,
                }
            ],
            theme_updates_json=[],
        ),
        plugins=(
            {
                "plugin_file": "kosmos-bridge/kosmos-bridge.php",
                "name": "Kosmos Bridge",
                "version": "0.3.52",
                "active": True,
            },
        ),
    )

    service = object.__new__(FleetInventoryService)
    service._attach_official_plugin_versions = lambda entries: entries
    entry = service.build_update_workbench([item])[0]

    assert entry.current_version == "0.3.56"
    assert entry.target_version == "0.3.58"


def test_fleet_observed_versions_are_informational_and_ignore_lower_offers():
    captured_at = datetime.now(UTC)
    service = object.__new__(FleetInventoryService)
    entries = [
        UpdateWorkbenchEntry(
            site=SimpleNamespace(id=1, domain="current.example"),
            kind="plugin",
            name="Borlabs Cookie",
            identifier="borlabs-cookie/borlabs-cookie.php",
            current_version="2.3",
            target_version="",
            is_active=True,
            update_available=False,
            update_checked=True,
            execution_ready=False,
            execution_note="",
            captured_at=captured_at,
        ),
        UpdateWorkbenchEntry(
            site=SimpleNamespace(id=2, domain="offered.example"),
            kind="plugin",
            name="Borlabs Cookie",
            identifier="borlabs-cookie/borlabs-cookie.php",
            current_version="2.2.63",
            target_version="2.2.68",
            is_active=True,
            update_available=True,
            update_checked=True,
            execution_ready=True,
            execution_note="",
            captured_at=captured_at,
        ),
        UpdateWorkbenchEntry(
            site=SimpleNamespace(id=3, domain="newer.example"),
            kind="plugin",
            name="Borlabs Cookie",
            identifier="borlabs-cookie/borlabs-cookie.php",
            current_version="2.3.7",
            target_version="",
            is_active=True,
            update_available=False,
            update_checked=True,
            execution_ready=False,
            execution_note="",
            captured_at=captured_at,
        ),
    ]

    enriched = service._attach_fleet_observed_versions(entries)

    assert enriched[0].fleet_observed_version == "2.3.7"
    assert enriched[0].fleet_observed_site_count == 1
    assert enriched[2].fleet_observed_version == ""


def test_status_refresh_combines_installed_state_and_update_checks():
    service = object.__new__(FleetInventoryService)
    service.refresh_verified_site_states = lambda *, limit: {"refreshed": [{"site_id": 1}], "failed": [], "skipped": []}
    service.refresh_verified_site_updates = lambda *, limit: {"refreshed": [{"site_id": 1}], "failed": [], "skipped": []}

    result = service.refresh_verified_site_statuses(limit=25)

    assert result["state"]["refreshed"] == [{"site_id": 1}]
    assert result["updates"]["refreshed"] == [{"site_id": 1}]


def test_direct_updates_reject_plugins_without_an_authorized_package():
    assert MaintenanceRunService._direct_plugin_update_scope_error(
        plugin_entry(execution_ready=False, execution_note="License activation is required.")
    ) == "License activation is required."


def test_direct_updates_accept_jet_plugin_with_stored_crocoblock_license():
    assert MaintenanceRunService._direct_plugin_update_scope_error(
        plugin_entry(
            name="JetElements",
            identifier="jet-elements/jet-elements.php",
            execution_ready=False,
        ),
        allow_stored_crocoblock_license=True,
        has_stored_crocoblock_license=True,
    ) is None


def test_direct_updates_reject_jet_plugin_without_stored_crocoblock_license():
    assert MaintenanceRunService._direct_plugin_update_scope_error(
        plugin_entry(
            name="JetElements",
            identifier="jet-elements/jet-elements.php",
            execution_ready=False,
        ),
        allow_stored_crocoblock_license=True,
    ) == "JetElements needs the centrally stored Crocoblock license before its update package is available."


def test_direct_updates_reject_unknown_update_entries():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry(kind="translation")) == (
        "Direct updates support WordPress core, themes, and plugins only."
    )


def test_official_version_comparison_marks_missing_site_update_offer():
    mismatch, note = OfficialPluginVersionService.comparison(
        current_version="3.22.1",
        reported_version="",
        official_version="3.22.2",
    )

    assert mismatch is True
    assert note == "Mismatch: installed 3.22.1; official version 3.22.2 has no site update offer."


def test_official_version_comparison_accepts_matching_reported_target():
    mismatch, note = OfficialPluginVersionService.comparison(
        current_version="3.22.1",
        reported_version="3.22.2",
        official_version="3.22.2",
    )

    assert mismatch is False
    assert note == "The reported update matches the official version."


def test_version_diagnosis_explains_a_missing_site_offer():
    status, label, note = OfficialPluginVersionService.diagnosis(
        current_version="3.22.1",
        reported_version="",
        official_version="3.22.2",
        official_source="WordPress.org",
        execution_ready=False,
        execution_note="",
        is_jet_plugin=False,
    )

    assert status == "site-offer-missing"
    assert label == "Site offer missing"
    assert "will not update" in note


def test_version_diagnosis_blocks_conflicting_provider_information():
    status, label, note = OfficialPluginVersionService.diagnosis(
        current_version="3.22.1",
        reported_version="3.22.3",
        official_version="3.22.2",
        official_source="WordPress.org",
        execution_ready=True,
        execution_note="",
        is_jet_plugin=False,
    )

    assert status == "provider-conflict"
    assert label == "Provider information conflicts"
    assert "will not update" in note


def test_version_diagnosis_explains_the_crocoblock_license_step():
    status, label, note = OfficialPluginVersionService.diagnosis(
        current_version="2.7.4.1",
        reported_version="2.9.2",
        official_version="2.9.2",
        official_source="Crocoblock Jet Dashboard",
        execution_ready=False,
        execution_note="Crocoblock must activate a valid license for this site before its update package is available.",
        is_jet_plugin=True,
    )

    assert status == "crocoblock-license-step"
    assert label == "Crocoblock license step"
    assert "activates the stored Crocoblock license" in note


def test_official_version_lookup_uses_the_documented_wordpress_org_query_shape():
    url = OfficialPluginVersionService.WORDPRESS_ORG_API.format(slug="wordpress-seo")

    assert url == "https://api.wordpress.org/plugins/info/1.2/?action=plugin_information&request[slug]=wordpress-seo"


def test_elementor_pro_changelog_parser_returns_the_newest_version_heading():
    changelog = """
        <h4>4.2.2 - 2026-08-19</h4>
        <h4>4.2.1 - 2026-07-28</h4>
    """

    assert OfficialPluginVersionService._parse_elementor_pro_version(changelog) == "4.2.2"


def test_elementor_pro_changelog_parser_rejects_unversioned_headings():
    assert OfficialPluginVersionService._parse_elementor_pro_version("<h4>Latest updates</h4>") is None


def test_crocoblock_changelog_lookup_maps_latest_versions_and_plugin_aliases(monkeypatch):
    class Response:
        def read(self):
            return (
                b'[{"name":"JetElements 2.9.2","slug":"jet-elements"},'
                b'{"name":"JetCompareWishlist 1.5.13","slug":"jet-cw"}]'
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("app.services.official_plugin_versions.urlopen", lambda *_args, **_kwargs: Response())

    lookup = OfficialPluginVersionService.fetch_crocoblock_changelog_versions(
        (
            "jet-elements/jet-elements.php",
            "jet-compare-wishlist/jet-compare-wishlist.php",
            "elementor-pro/elementor-pro.php",
        )
    )

    assert lookup.requested == 2
    assert lookup.error is None
    assert lookup.versions == {
        "jet-elements/jet-elements.php": "2.9.2",
        "jet-compare-wishlist/jet-compare-wishlist.php": "1.5.13",
    }


def test_crocoblock_changelog_parser_rejects_entries_without_a_version():
    assert OfficialPluginVersionService._parse_crocoblock_changelog_version("JetElements current") is None


def test_automatic_crocoblock_activation_candidates_require_an_unavailable_update_package():
    entries = [
        SimpleNamespace(
            kind="plugin",
            identifier="jet-elements/jet-elements.php",
            update_available=True,
            target_version="2.9.2",
            execution_ready=True,
            site=SimpleNamespace(id=1),
        ),
        SimpleNamespace(
            kind="plugin",
            identifier="jet-engine/jet-engine.php",
            update_available=True,
            target_version="3.8.13",
            execution_ready=False,
            site=SimpleNamespace(id=2),
        ),
        SimpleNamespace(
            kind="plugin",
            identifier="jet-tabs/jet-tabs.php",
            update_available=False,
            target_version="2.3.2",
            execution_ready=False,
            site=SimpleNamespace(id=3),
        ),
    ]

    selected = FleetRefreshService._jet_sites_requiring_provider(
        entries=entries,
    )

    assert selected == {2}


def test_official_version_refresh_uses_elementor_pro_changelog_once_per_catalogue_check():
    records = []
    service = object.__new__(OfficialPluginVersionService)
    service.db = SimpleNamespace(add=records.append, flush=lambda: None)
    service._collect_candidates = lambda _items: {
        "elementor-pro/elementor-pro.php": SimpleNamespace(plugin_file="elementor-pro/elementor-pro.php"),
        "wordpress-seo/wp-seo.php": SimpleNamespace(plugin_file="wordpress-seo/wp-seo.php"),
    }
    service.get_cached = lambda _candidates: {}
    wordpress_org_requests = []
    service._fetch_wordpress_org_version = lambda plugin_file: (wordpress_org_requests.append(plugin_file) or ("25.1", None))
    service._fetch_elementor_pro_version = lambda: ("4.2.2", None)

    summary = service.refresh_for_inventory([])

    assert summary["checked"] == 2
    assert summary["completed"] == 2
    assert summary["elementor_pro"] == 1
    assert wordpress_org_requests == ["wordpress-seo/wp-seo.php"]
    elementor_record = next(record for record in records if record.plugin_file == "elementor-pro/elementor-pro.php")
    assert elementor_record.official_version == "4.2.2"
    assert elementor_record.source == "Elementor Pro Changelog"


def test_pafe_pro_changelog_parser_returns_the_newest_version():
    changelog = """
        <h2>[PRO] 7.1.73 (2026/06/23)</h2>
        <h2>[PRO] 7.1.72 (2026/06/11)</h2>
    """

    assert OfficialPluginVersionService._parse_pafe_pro_version(changelog) == "7.1.73"


def test_official_version_refresh_uses_pafe_pro_changelog_once_per_catalogue_check():
    records = []
    service = object.__new__(OfficialPluginVersionService)
    service.db = SimpleNamespace(add=records.append, flush=lambda: None)
    service._collect_candidates = lambda _items: {
        "piotnet-addons-for-elementor-pro/piotnet-addons-for-elementor-pro.php": SimpleNamespace(
            plugin_file="piotnet-addons-for-elementor-pro/piotnet-addons-for-elementor-pro.php"
        ),
        "wordpress-seo/wp-seo.php": SimpleNamespace(plugin_file="wordpress-seo/wp-seo.php"),
    }
    service.get_cached = lambda _candidates: {}
    wordpress_org_requests = []
    service._fetch_wordpress_org_version = lambda plugin_file: (wordpress_org_requests.append(plugin_file) or ("25.1", None))
    service._fetch_pafe_pro_version = lambda: ("7.1.73", None)

    summary = service.refresh_for_inventory([])

    assert summary["checked"] == 2
    assert summary["completed"] == 2
    assert summary["pafe_pro"] == 1
    assert wordpress_org_requests == ["wordpress-seo/wp-seo.php"]
    pafe_record = next(
        record
        for record in records
        if record.plugin_file == "piotnet-addons-for-elementor-pro/piotnet-addons-for-elementor-pro.php"
    )
    assert pafe_record.official_version == "7.1.73"
    assert pafe_record.source == "PAFE Pro Changelog"


def test_official_version_refresh_uses_kosmos_bridge_metadata_once_per_catalogue_check():
    records = []
    service = object.__new__(OfficialPluginVersionService)
    service.db = SimpleNamespace(add=records.append, flush=lambda: None)
    service._collect_candidates = lambda _items: {
        "kosmos-bridge/kosmos-bridge.php": SimpleNamespace(plugin_file="kosmos-bridge/kosmos-bridge.php"),
        "wordpress-seo/wp-seo.php": SimpleNamespace(plugin_file="wordpress-seo/wp-seo.php"),
    }
    service.get_cached = lambda _candidates: {}
    wordpress_org_requests = []
    service._fetch_wordpress_org_version = lambda plugin_file: (wordpress_org_requests.append(plugin_file) or ("25.1", None))
    service._fetch_kosmos_bridge_version = lambda: ("0.3.58", None)

    summary = service.refresh_for_inventory([])

    assert summary["checked"] == 2
    assert summary["completed"] == 2
    assert summary["kosmos_bridge"] == 1
    assert wordpress_org_requests == ["wordpress-seo/wp-seo.php"]
    bridge_record = next(record for record in records if record.plugin_file == "kosmos-bridge/kosmos-bridge.php")
    assert bridge_record.official_version == "0.3.58"
    assert bridge_record.source == "Kosmos Bridge update metadata"


def test_kosmos_bridge_catalogue_offer_replaces_an_older_site_offer():
    captured_at = datetime.now(UTC)
    entry = UpdateWorkbenchEntry(
        site=SimpleNamespace(id=1, domain="example.test"),
        kind="plugin",
        name="Kosmos Bridge",
        identifier="kosmos-bridge/kosmos-bridge.php",
        current_version="0.3.52",
        target_version="0.3.56",
        is_active=True,
        update_available=True,
        update_checked=True,
        execution_ready=True,
        execution_note="",
        captured_at=captured_at,
        official_version="0.3.58",
        official_source="Kosmos Bridge update metadata",
        official_checked_at=captured_at,
        official_mismatch=True,
    )

    offered = FleetInventoryService._offer_kosmos_bridge_updates([entry])[0]

    assert offered.target_version == "0.3.58"
    assert offered.update_available is True
    assert offered.direct_update_selectable is True
    assert offered.official_mismatch is False
    assert offered.diagnosis_status == "update-ready"


def test_provider_license_normalization_keeps_elementor_in_one_row():
    assert ProviderCredentialService.normalize_provider("Elementor Pro") == "elementor"
    assert ProviderCredentialService.normalize_provider(" Elementor ") == "elementor"


def test_crocoblock_version_evidence_does_not_consider_non_jet_plugin_sites():
    assert CrocoblockLicenseService.is_jet_plugin_file("jet-elements/jet-elements.php") is True
    assert CrocoblockLicenseService.is_jet_plugin_file("elementor-pro/elementor-pro.php") is False


def test_crocoblock_provider_versions_ignore_invalid_catalog_entries():
    versions = CrocoblockLicenseService._provider_versions(
        {
            "plugins": [
                {"plugin_file": "jet-elements/jet-elements.php", "version": "2.7.3"},
                {"plugin_file": "elementor-pro/elementor-pro.php", "version": "3.30.0"},
                {"plugin_file": "jet-engine/jet-engine.php", "version": ""},
            ]
        }
    )

    assert versions == [{"plugin_file": "jet-elements/jet-elements.php", "version": "2.7.3"}]


def test_crocoblock_dashboard_diagnostic_keeps_only_safe_compatibility_fields():
    service = object.__new__(CrocoblockLicenseService)
    service.proxy = SimpleNamespace(
        execute_readonly_ability=lambda *_args, **_kwargs: {
            "result": {
                "bootstrap_hooks_found": 1,
                "bootstrap_hooks_invoked": 1,
                "bootstrap_hook_errors": 0,
                "dashboard_class_available": True,
                "utils_class_available": True,
                "dashboard_instance_available": True,
                "init_managers_available": True,
                "init_managers_called": True,
                "init_managers_failed": False,
                "license_manager_available": False,
                "license_manager_methods": ["license_action_query", "private_value"],
                "plugin_manager_available": True,
                "plugin_manager_methods": ["get_remote_jet_plugin_list"],
                "message": "Jet Dashboard initialized, but it did not expose a license manager.",
                "license_key": "must-not-be-stored",
            }
        }
    )

    diagnostic = service._diagnose_crocoblock_dashboard(42)

    assert diagnostic == {
        "status": "captured",
        "message": "Jet Dashboard initialized, but it did not expose a license manager.",
        "dashboard_class_available": True,
        "utils_class_available": True,
        "dashboard_instance_available": True,
        "init_managers_available": True,
        "init_managers_called": True,
        "init_managers_failed": False,
        "license_manager_available": False,
        "plugin_manager_available": True,
        "bootstrap_hooks_found": 1,
        "bootstrap_hooks_invoked": 1,
        "bootstrap_hook_errors": 0,
        "license_manager_methods": ["license_action_query"],
        "plugin_manager_methods": ["get_remote_jet_plugin_list"],
    }


def test_crocoblock_dashboard_diagnostic_reports_when_bridge_update_is_needed():
    service = object.__new__(CrocoblockLicenseService)

    def missing_ability(*_args, **_kwargs):
        raise SiteMcpProxyError("KOSMOS_BRIDGE_ABILITY_NOT_FOUND", "Ability not found.", status_code=404)

    service.proxy = SimpleNamespace(execute_readonly_ability=missing_ability)

    diagnostic = service._diagnose_crocoblock_dashboard(42)

    assert diagnostic["status"] == "bridge-upgrade-required"
    assert "0.3.57" in diagnostic["message"]


def test_direct_updates_require_healthy_homepage_rest_api_and_supported_admin_ajax():
    assert MaintenanceRunService._plugin_update_health_error(
        {
            "home_healthy": True,
            "home_status": 200,
            "rest_healthy": True,
            "rest_status": 200,
            "admin_ajax_healthy": True,
            "admin_ajax_status": 200,
        }
    ) is None
    assert MaintenanceRunService._plugin_update_health_error(
        {"home_healthy": False, "home_status": 503, "rest_healthy": True, "rest_status": 200}
    ) == "the public homepage health check did not pass (HTTP 503)"
    assert MaintenanceRunService._plugin_update_health_error(
        {
            "home_healthy": True,
            "home_status": 200,
            "rest_healthy": True,
            "rest_status": 200,
            "admin_ajax_healthy": False,
            "admin_ajax_status": 500,
        }
    ) == "the WordPress admin AJAX health check did not pass (HTTP 500)"
    access_policy_result = {
        "home_healthy": True,
        "home_status": 200,
        "rest_healthy": True,
        "rest_status": 200,
        "admin_ajax_healthy": False,
        "admin_ajax_status": 403,
    }
    assert MaintenanceRunService._plugin_update_health_error(access_policy_result) is None
    assert MaintenanceRunService._post_update_health_failure_kind(access_policy_result) is None
    assert "access policy" in MaintenanceRunService._plugin_update_health_detail(access_policy_result)


def test_stale_direct_update_postflight_is_recovered_without_repeating_the_update(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    health_calls = []

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        health_calls.append((site_id, ability_name, ability_input, timeout_seconds))
        assert ability_name == MaintenanceRunService.SITE_HEALTH_ABILITY
        return {
            "result": {
                "home_healthy": True,
                "home_status": 200,
                "rest_healthy": True,
                "rest_status": 200,
                "admin_ajax_healthy": False,
                "admin_ajax_status": 403,
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    monkeypatch.setattr(MaintenanceRunService, "_record_confirmed_direct_update", lambda *_args: None)

    with Session(engine) as db:
        site = Site(
            uuid="87654321-1234-1234-1234-123456789012",
            domain="stale-postflight.example",
            home_url="https://stale-postflight.example",
            site_url="https://stale-postflight.example",
            status=SiteStatus.verified.value,
        )
        run = MaintenanceRun(
            site=site,
            kind=MaintenanceRunService.PLUGIN_UPDATE_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by="operator",
            started_at=now - timedelta(minutes=6),
            last_checked_at=now - timedelta(minutes=6),
            result_json={
                "batch_id": "a" * 32,
                "stage": "postflight-health",
                "update_kind": "plugin",
                "update_identifier": "kosmos-bridge/kosmos-bridge.php",
                "update_name": "Kosmos Bridge",
                "current_version": "0.3.60",
                "target_version": "0.3.61",
                "expected_active": True,
            },
        )
        run.steps.extend(
            [
                MaintenanceRunStep(
                    step_key="update-plugin",
                    status=MaintenanceRunStepStatus.succeeded.value,
                    started_at=now - timedelta(minutes=6),
                    completed_at=now - timedelta(minutes=6),
                    result_json={
                        "updated": True,
                        "plugin_file": "kosmos-bridge/kosmos-bridge.php",
                        "previous_version": "0.3.60",
                        "installed_version": "0.3.61",
                        "active": True,
                    },
                ),
                MaintenanceRunStep(
                    step_key="postflight-health",
                    status=MaintenanceRunStepStatus.running.value,
                    started_at=now - timedelta(minutes=6),
                    result_json={},
                ),
            ]
        )
        db.add(run)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=None)
        assert service.recover_stale_direct_update_postflights() == {
            "succeeded": 1,
            "failed": 0,
            "waiting": 0,
        }

        db.refresh(run)
        assert run.status == MaintenanceRunStatus.succeeded.value
        assert run.result_json["stage"] == "completed"
        assert run.result_json["installed_version"] == "0.3.61"
        assert run.steps[1].status == MaintenanceRunStepStatus.succeeded.value
        assert health_calls == [(site.id, MaintenanceRunService.SITE_HEALTH_ABILITY, None, 45)]


def test_framework_stabilization_is_limited_to_wordpress_and_active_framework_plugins():
    assert MaintenanceRunService._requires_post_update_framework_stabilization(
        {"update_kind": "wordpress", "update_identifier": "wordpress-core"}
    ) is True
    assert MaintenanceRunService._requires_post_update_framework_stabilization(
        {
            "update_kind": "plugin",
            "update_identifier": "jet-engine/jet-engine.php",
            "expected_active": True,
        }
    ) is True
    assert MaintenanceRunService._requires_post_update_framework_stabilization(
        {
            "update_kind": "plugin",
            "update_identifier": "jet-engine/jet-engine.php",
            "expected_active": False,
        }
    ) is False
    assert MaintenanceRunService._requires_post_update_framework_stabilization(
        {
            "update_kind": "plugin",
            "update_identifier": "akismet/akismet.php",
            "expected_active": True,
        }
    ) is False


def test_plugin_installation_requires_the_checked_file_version_and_requested_activation():
    details = {
        "plugin_name": "Sample Plugin",
        "plugin_file": "sample-plugin/sample-plugin.php",
        "target_version": "1.2.3",
        "activate": True,
    }

    assert MaintenanceRunService._plugin_installation_result_error(
        details,
        {
            "installed": True,
            "plugin_file": "sample-plugin/sample-plugin.php",
            "installed_version": "1.2.3",
            "active": True,
        },
    ) is None
    assert "did not return the checked package version" in MaintenanceRunService._plugin_installation_result_error(
        details,
        {
            "installed": True,
            "plugin_file": "sample-plugin/sample-plugin.php",
            "installed_version": "1.2.2",
            "active": True,
        },
    )
    assert "did not confirm activation" in MaintenanceRunService._plugin_installation_result_error(
        details,
        {
            "installed": True,
            "plugin_file": "sample-plugin/sample-plugin.php",
            "installed_version": "1.2.3",
            "active": False,
        },
    )


def test_direct_update_health_check_retries_a_transient_bridge_error():
    class Proxy:
        def __init__(self):
            self.calls = 0

        def execute_ability(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise SiteMcpProxyError("REMOTE_ERROR", "temporary WordPress initialization error")
            return {
                "result": {
                    "home_healthy": True,
                    "home_status": 200,
                    "rest_healthy": True,
                    "rest_status": 200,
                }
            }

    service = object.__new__(MaintenanceRunService)
    service.proxy = Proxy()
    service.POST_UPDATE_HEALTH_MAX_ATTEMPTS = 3
    service.POST_UPDATE_HEALTH_RETRY_DELAY_SECONDS = 0
    progress = []
    service._start_plugin_update_step = lambda _run, _step, detail: progress.append(detail)

    error, detail, result = service._run_direct_update_postflight_health(
        SimpleNamespace(site_id=17),
        None,
    )

    assert error is None
    assert detail == "Post-update health check: homepage HTTP 200; WordPress REST API HTTP 200."
    assert result["home_healthy"] is True
    assert service.proxy.calls == 2
    assert len(progress) == 1


def test_fleet_refresh_result_uses_the_runtime_settings_snapshot():
    result = FleetRefreshService._initial_result(
        FleetRefreshService.MODE_NORMAL,
        runtime_settings=FleetRefreshRuntimeSettings(
            max_parallel_site_checks=4,
        ),
    )

    assert result["settings"] == {
        "max_parallel_site_checks": 4,
    }


def test_official_version_refresh_rechecks_catalogues_even_when_a_previous_result_exists():
    existing = SimpleNamespace(
        plugin_file="akismet/akismet.php",
        official_version="5.3",
        source="WordPress.org",
        checked_at=datetime.now(UTC),
        last_error=None,
    )
    service = object.__new__(OfficialPluginVersionService)
    service.db = SimpleNamespace(add=lambda _record: None, flush=lambda: None)
    service._collect_candidates = lambda _items: {
        "akismet/akismet.php": SimpleNamespace(plugin_file="akismet/akismet.php"),
    }
    service.get_cached = lambda _candidates: {existing.plugin_file: existing}
    service._fetch_wordpress_org_version = lambda _plugin_file: ("5.4", None)

    summary = service.refresh_for_inventory([])

    assert summary["checked"] == 1
    assert summary["cached"] == 0
    assert existing.official_version == "5.4"


def test_official_version_refresh_reports_each_completed_catalogue_check():
    service = object.__new__(OfficialPluginVersionService)
    service.db = SimpleNamespace(add=lambda _record: None, flush=lambda: None)
    service._collect_candidates = lambda _items: {
        "first/first.php": SimpleNamespace(plugin_file="first/first.php"),
        "second/second.php": SimpleNamespace(plugin_file="second/second.php"),
    }
    service.get_cached = lambda _candidates: {}
    service._fetch_wordpress_org_version = lambda plugin_file: ("1.2.3", None)
    progress = []

    summary = service.refresh_for_inventory([], progress_callback=progress.append)

    assert summary["checked"] == 2
    assert summary["completed"] == 2
    assert [entry["completed"] for entry in progress] == [0, 1, 2]


def test_update_workbench_filters_by_diagnosis_and_attention():
    service = object.__new__(FleetInventoryService)
    entries = [
        SimpleNamespace(
            kind="plugin",
            is_active=True,
            diagnosis_status="provider-conflict",
            official_mismatch=True,
            site=SimpleNamespace(domain="first.example"),
            name="First",
            identifier="first/first.php",
        ),
        SimpleNamespace(
            kind="plugin",
            is_active=True,
            diagnosis_status="aligned",
            official_mismatch=False,
            site=SimpleNamespace(domain="second.example"),
            name="Second",
            identifier="second/second.php",
        ),
    ]

    assert service.filter_update_workbench(entries, diagnosis="attention") == [entries[0]]
    assert service.filter_update_workbench(entries, diagnosis="aligned") == [entries[1]]


def test_update_workbench_filters_by_site_and_plugin():
    service = object.__new__(FleetInventoryService)
    entries = [
        SimpleNamespace(
            kind="plugin",
            is_active=True,
            diagnosis_status="aligned",
            official_mismatch=False,
            site=SimpleNamespace(id=11, domain="first.example"),
            name="First",
            identifier="first/first.php",
        ),
        SimpleNamespace(
            kind="plugin",
            is_active=True,
            diagnosis_status="aligned",
            official_mismatch=False,
            site=SimpleNamespace(id=12, domain="second.example"),
            name="Second",
            identifier="second/second.php",
        ),
    ]

    assert service.filter_update_workbench(entries, site_id=11) == [entries[0]]
    assert service.filter_update_workbench(entries, plugin_identifier="second/second.php") == [entries[1]]
