from starlette.requests import Request
from starlette.templating import Jinja2Templates
from sqlalchemy import func, select

from app.core.csrf import get_csrf_token
from app.core.timezones import format_berlin_time
from app.db.session import SessionLocal
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.site_admin_launch import SiteAdminLaunchService
from app.services.styling_settings import StylingRuntimeSettings, StylingSettingsService


def _shared_template_context(request: Request) -> dict[str, object]:
    user = getattr(request.state, "hub_user", None)
    return {
        "csrf_token": get_csrf_token(request),
        "can_launch_wordpress_admin": user is not None and user.role == "admin",
        "unread_email_count": _unread_email_count(user),
        "styling": _styling_settings(),
    }


def _styling_settings() -> StylingRuntimeSettings:
    """Keep templates available during startup even before the singleton table exists."""
    try:
        with SessionLocal() as db:
            return StylingSettingsService(db=db).get_runtime_settings()
    except Exception:
        return StylingRuntimeSettings()


def _unread_email_count(user) -> int:
    if user is None:
        return 0
    try:
        with SessionLocal() as db:
            return _unread_email_count_for_db(db)
    except Exception:
        return 0


def _unread_email_count_for_db(db) -> int:
    """Count unread messages without loading encrypted email payloads for the navigation badge."""
    linked_filters = (
        CustomerZohoEmail.direction == "inbound",
        CustomerZohoEmail.is_unread.is_(True),
    )
    # A Zoho message can be linked to several customers through shared contacts.
    linked_count = int(
        db.scalar(
            select(func.count(func.distinct(CustomerZohoEmail.zoho_message_id))).where(
                *linked_filters,
                CustomerZohoEmail.zoho_message_id.is_not(None),
            )
        )
        or 0
    )
    local_linked_count = int(
        db.scalar(
            select(func.count())
            .select_from(CustomerZohoEmail)
            .where(*linked_filters, CustomerZohoEmail.zoho_message_id.is_(None))
        )
        or 0
    )
    unassigned_count = int(
        db.scalar(
            select(func.count())
            .select_from(HubMailboxEmail)
            .where(
                HubMailboxEmail.direction == "inbound",
                HubMailboxEmail.is_unread.is_(True),
            )
        )
        or 0
    )
    return linked_count + local_linked_count + unassigned_count


def create_templates(*, directory: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=directory, context_processors=[_shared_template_context])
    templates.env.filters["berlin_time"] = format_berlin_time
    templates.env.globals["bridge_supports_admin_launch"] = SiteAdminLaunchService.bridge_supports_launch
    return templates
