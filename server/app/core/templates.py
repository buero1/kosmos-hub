from starlette.requests import Request
from starlette.templating import Jinja2Templates

from app.core.csrf import get_csrf_token
from app.core.timezones import format_berlin_time
from app.services.site_admin_launch import SiteAdminLaunchService


def _shared_template_context(request: Request) -> dict[str, object]:
    user = getattr(request.state, "hub_user", None)
    return {
        "csrf_token": get_csrf_token(request),
        "can_launch_wordpress_admin": user is not None and user.role == "admin",
    }


def create_templates(*, directory: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=directory, context_processors=[_shared_template_context])
    templates.env.filters["berlin_time"] = format_berlin_time
    templates.env.globals["bridge_supports_admin_launch"] = SiteAdminLaunchService.bridge_supports_launch
    return templates
