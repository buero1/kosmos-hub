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
            linked_emails = db.scalars(
                select(CustomerZohoEmail).where(
                    CustomerZohoEmail.direction == "inbound",
                    CustomerZohoEmail.is_unread.is_(True),
                )
            ).all()
            # One Zoho email can be linked to several customers through shared contacts.
            linked_count = len({email.zoho_message_id or f"local-{email.id}" for email in linked_emails})
            return linked_count + int(
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
    except Exception:
        return 0


def create_templates(*, directory: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=directory, context_processors=[_shared_template_context])
    templates.env.filters["berlin_time"] = format_berlin_time
    templates.env.globals["bridge_supports_admin_launch"] = SiteAdminLaunchService.bridge_supports_launch
    return templates
