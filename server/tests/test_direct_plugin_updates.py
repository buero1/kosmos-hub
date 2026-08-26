from types import SimpleNamespace

from app.services.maintenance_runs import MaintenanceRunService


def plugin_entry(**overrides):
    values = {
        "kind": "plugin",
        "name": "Smush",
        "identifier": "wp-smushit/wp-smush.php",
        "is_active": True,
        "current_version": "3.22.1",
        "target_version": "3.22.2",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_direct_updates_accept_active_plugin_with_exact_versions():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry()) is None


def test_direct_updates_reject_inactive_plugins():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry(is_active=False)) == (
        "Smush is inactive. Direct updates currently require an active plugin."
    )


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
