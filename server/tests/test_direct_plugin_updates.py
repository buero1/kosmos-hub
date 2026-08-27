from datetime import UTC, datetime, timedelta
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.fleet_inventory import FleetInventoryItem, FleetInventoryService, UpdateWorkbenchEntry
from app.services.fleet_refresh import FleetRefreshService
from app.services.fleet_refresh_settings import FleetRefreshRuntimeSettings
from app.services.maintenance_runs import MaintenanceRunService
from app.services.official_plugin_versions import OfficialPluginVersionService
from app.services.provider_credentials import ProviderCredentialService
from app.services.crocoblock_license import CrocoblockLicenseService
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


def test_direct_updates_default_to_five_parallel_customer_sites():
    assert FleetRefreshRuntimeSettings().max_parallel_direct_updates == 5


def test_direct_updates_accept_inactive_plugins():
    assert MaintenanceRunService._direct_plugin_update_scope_error(plugin_entry(is_active=False)) is None


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


def test_legacy_site_update_provider_version_is_never_reused_as_official_cache():
    record = SimpleNamespace(source="Site update provider: wordpress", checked_at=datetime.now(UTC))

    assert OfficialPluginVersionService._is_fresh(
        record,
        now=datetime.now(UTC),
        max_age=timedelta(hours=24),
    ) is False


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


def test_fleet_refresh_result_uses_the_runtime_settings_snapshot():
    result = FleetRefreshService._initial_result(
        FleetRefreshService.MODE_NORMAL,
        runtime_settings=FleetRefreshRuntimeSettings(
            site_status_max_age_minutes=20,
            official_version_max_age_hours=36,
            max_parallel_site_checks=4,
        ),
    )

    assert result["settings"] == {
        "site_status_max_age_minutes": 20,
        "official_version_max_age_hours": 36,
        "max_parallel_site_checks": 4,
    }


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
