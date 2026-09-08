from pathlib import Path

from app.api.routes.web import router
import app.mcp_server as mcp_server


def test_update_plan_web_routes_are_not_registered():
    paths = {route.path for route in router.routes}

    assert not any(path.startswith("/update-plans") for path in paths)


def test_update_plans_are_not_linked_in_the_navigation():
    base_template = Path(__file__).parents[1] / "app" / "templates" / "base.html"

    assert "/update-plans" not in base_template.read_text(encoding="utf-8")


def test_update_plan_mcp_actions_are_not_available():
    assert not hasattr(mcp_server, "get_update_plan")
    assert not hasattr(mcp_server, "approve_plugin_update_plan")
    assert not hasattr(mcp_server, "execute_approved_plugin_update_plan")
