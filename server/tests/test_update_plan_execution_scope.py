from types import SimpleNamespace

from app.services.update_plans import UpdatePlanService


def mainwp_child_plan(**overrides):
    values = {
        "update_type": "plugin",
        "update_identifier": "mainwp-child/mainwp-child.php",
        "update_name": "MainWP Child",
        "current_version": "5.4.1",
        "target_version": "5.4.2",
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(items=[SimpleNamespace(**values)])


def test_execution_scope_accepts_one_active_mainwp_child_update():
    plan = mainwp_child_plan()

    assert UpdatePlanService.mainwp_child_scope_error(UpdatePlanService, plan) is None


def test_execution_scope_rejects_other_plugins():
    plan = mainwp_child_plan(update_identifier="woocommerce/woocommerce.php", update_name="WooCommerce")

    assert UpdatePlanService.mainwp_child_scope_error(UpdatePlanService, plan) == (
        "This execution path is restricted to the MainWP Child plugin."
    )


def test_execution_scope_rejects_inactive_mainwp_child():
    plan = mainwp_child_plan(is_active=False)

    assert UpdatePlanService.mainwp_child_scope_error(UpdatePlanService, plan) == (
        "MainWP Child must be active before it can be updated by the Hub."
    )


def test_recovery_scope_accepts_inactive_mainwp_child_at_planned_version():
    plan = mainwp_child_plan(is_active=False)

    assert UpdatePlanService.mainwp_child_recovery_scope_error(UpdatePlanService, plan) is None


def test_recovery_scope_rejects_other_plugins():
    plan = mainwp_child_plan(update_identifier="woocommerce/woocommerce.php", update_name="WooCommerce")

    assert UpdatePlanService.mainwp_child_recovery_scope_error(UpdatePlanService, plan) == (
        "This recovery path is restricted to the MainWP Child plugin."
    )


def test_execution_scope_rejects_multi_item_plans():
    plan = mainwp_child_plan()
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

    assert UpdatePlanService.mainwp_child_scope_error(UpdatePlanService, plan) == (
        "This execution path only accepts a plan with exactly one update."
    )
