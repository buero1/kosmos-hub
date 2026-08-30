from datetime import UTC, datetime
from types import SimpleNamespace

from app.models.audit_log import AuditLog
from app.models.customer import Customer
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


def test_direct_update_preflight_stops_when_the_installed_version_changed():
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

    assert error == "Kosmos Bridge changed installed version since it was selected. Refresh the workbench and start a new run."
    assert note == ""


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
            site_status_max_age_minutes=20,
            max_parallel_site_checks=4,
        ),
    )

    assert result["settings"] == {
        "site_status_max_age_minutes": 20,
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
