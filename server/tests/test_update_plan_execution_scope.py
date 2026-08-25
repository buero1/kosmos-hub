from types import SimpleNamespace

from app.services.update_plans import UpdatePlanService


def plugin_update_plan(**overrides):
    values = {
        "update_type": "plugin",
        "update_identifier": "mainwp-child/mainwp-child.php",
        "update_name": "MainWP Child",
        "current_version": "5.4.1",
        "target_version": "5.4.2",
        "is_active": True,
        "site": SimpleNamespace(domain="test-gasthofloewen.kosmos-medien.de"),
    }
    values.update(overrides)
    return SimpleNamespace(items=[SimpleNamespace(**values)])


def test_execution_scope_accepts_one_active_plugin_update():
    plan = plugin_update_plan()

    assert UpdatePlanService.plugin_update_scope_error(UpdatePlanService, plan) is None


def test_execution_scope_accepts_other_active_plugins():
    plan = plugin_update_plan(update_identifier="wp-smushit/wp-smush.php", update_name="Smush")

    assert UpdatePlanService.plugin_update_scope_error(UpdatePlanService, plan) is None


def test_execution_scope_rejects_inactive_plugin():
    plan = plugin_update_plan(is_active=False)

    assert UpdatePlanService.plugin_update_scope_error(UpdatePlanService, plan) == (
        "The selected plugin must be active before it can be updated by the Hub."
    )


def test_execution_scope_rejects_invalid_plugin_file():
    plan = plugin_update_plan(update_identifier="../wp-config.php")

    assert UpdatePlanService.plugin_update_scope_error(UpdatePlanService, plan) == (
        "This execution path accepts one standard WordPress plugin update only."
    )


def test_recovery_scope_accepts_inactive_plugin_at_planned_version():
    plan = plugin_update_plan(is_active=False)

    assert UpdatePlanService.plugin_recovery_scope_error(UpdatePlanService, plan) is None


def test_recovery_scope_rejects_non_plugin_entries():
    plan = plugin_update_plan(update_type="theme", update_identifier="twentytwentyfive/style.css")

    assert UpdatePlanService.plugin_recovery_scope_error(UpdatePlanService, plan) == (
        "This recovery path accepts one standard WordPress plugin only."
    )


def test_execution_scope_rejects_multi_item_plans():
    plan = plugin_update_plan()
    plan.items.append(
        SimpleNamespace(
            update_type="plugin",
            update_identifier="akismet/akismet.php",
            update_name="Akismet",
            current_version="1.0.0",
            target_version="1.0.1",
            is_active=True,
        )
    )

    assert UpdatePlanService.plugin_update_scope_error(UpdatePlanService, plan) == (
        "This execution path only accepts a plan with exactly one update."
    )


def test_postflight_accepts_healthy_homepage_and_rest_api():
    result = {"home_healthy": True, "home_status": 200, "rest_healthy": True, "rest_status": 200}

    assert UpdatePlanService._postflight_health_error(result) is None


def test_postflight_flags_homepage_health_failure():
    result = {"home_healthy": False, "home_status": 503, "rest_healthy": True, "rest_status": 200}

    assert UpdatePlanService._postflight_health_error(result) == (
        "the public homepage health check did not pass (HTTP 503)"
    )


def test_postflight_flags_missing_result():
    assert UpdatePlanService._postflight_health_error(None) == (
        "the Bridge did not return a verifiable health result"
    )


def test_mcp_confirmation_requires_the_planned_site_and_plugin_file():
    plan = plugin_update_plan(update_identifier="wp-smushit/wp-smush.php")

    assert UpdatePlanService.plugin_update_confirmation_error(
        UpdatePlanService,
        plan,
        confirmed_site="test-gasthofloewen.kosmos-medien.de",
        confirmed_plugin_file="wp-smushit/wp-smush.php",
    ) is None
    assert UpdatePlanService.plugin_update_confirmation_error(
        UpdatePlanService,
        plan,
        confirmed_site="another-site.example",
        confirmed_plugin_file="wp-smushit/wp-smush.php",
    ) == "The confirmed site does not match the site recorded in this update plan."
