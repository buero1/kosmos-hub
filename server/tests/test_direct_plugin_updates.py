from datetime import UTC, datetime, timedelta
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


def backup_snapshot(**overrides):
    values = {
        "provider_installed": True,
        "provider_active": True,
        "backup_available": True,
        "backup_complete": True,
        "backup_at": datetime.now(UTC),
        "summary_json": {"retention_protected": True},
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


def test_direct_updates_require_a_protected_backup():
    assert MaintenanceRunService._direct_backup_preflight_error(
        backup_snapshot(summary_json={"retention_protected": False})
    ) == "Direct update blocked: the latest complete backup is not protected from automatic deletion."


def test_direct_updates_reject_a_backup_older_than_seven_days():
    assert MaintenanceRunService._direct_backup_preflight_error(
        backup_snapshot(backup_at=datetime.now(UTC) - timedelta(days=8))
    ) == "Direct update blocked: the latest protected backup is older than seven days."


def test_direct_updates_require_healthy_homepage_and_rest_api():
    assert MaintenanceRunService._plugin_update_health_error(
        {"home_healthy": True, "home_status": 200, "rest_healthy": True, "rest_status": 200}
    ) is None
    assert MaintenanceRunService._plugin_update_health_error(
        {"home_healthy": False, "home_status": 503, "rest_healthy": True, "rest_status": 200}
    ) == "the public homepage health check did not pass (HTTP 503)"
