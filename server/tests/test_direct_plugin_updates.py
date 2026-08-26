from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.fleet_inventory import FleetInventoryItem, FleetInventoryService
from app.services.maintenance_runs import MaintenanceRunService
from app.services.official_plugin_versions import OfficialPluginVersionService
from app.services.provider_credentials import ProviderCredentialService
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


def test_direct_updates_accept_inactive_plugins():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry(is_active=False)) is None


def test_direct_update_details_preserve_an_inactive_selection():
    details = MaintenanceRunService._plugin_update_details(
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
        "Direct updates currently support WordPress plugins only."
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


def test_official_version_lookup_uses_the_documented_wordpress_org_query_shape():
    url = OfficialPluginVersionService.WORDPRESS_ORG_API.format(slug="wordpress-seo")

    assert url == "https://api.wordpress.org/plugins/info/1.2/?action=plugin_information&request[slug]=wordpress-seo"


def test_provider_license_normalization_keeps_elementor_in_one_row():
    assert ProviderCredentialService.normalize_provider("Elementor Pro") == "elementor"
    assert ProviderCredentialService.normalize_provider(" Elementor ") == "elementor"


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
