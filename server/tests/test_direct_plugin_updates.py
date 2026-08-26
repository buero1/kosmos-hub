from types import SimpleNamespace

from app.services.maintenance_runs import MaintenanceRunService
from app.services.site_mcp_proxy import SiteMcpProxyError


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


def test_direct_updates_reject_inactive_plugins():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry(is_active=False)) == (
        "Smush is inactive. Direct updates currently require an active plugin."
    )


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


def test_direct_updates_reject_non_plugin_entries():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry(kind="theme")) == (
        "Direct updates currently support active WordPress plugins only."
    )


def test_direct_updates_require_healthy_homepage_and_rest_api():
    assert MaintenanceRunService._plugin_update_health_error(
        {"home_healthy": True, "home_status": 200, "rest_healthy": True, "rest_status": 200}
    ) is None
    assert MaintenanceRunService._plugin_update_health_error(
        {"home_healthy": False, "home_status": 503, "rest_healthy": True, "rest_status": 200}
    ) == "the public homepage health check did not pass (HTTP 503)"


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
