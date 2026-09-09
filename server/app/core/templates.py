import re

from starlette.requests import Request
from starlette.templating import Jinja2Templates
from sqlalchemy import func, select

from app.core.csrf import get_csrf_token
from app.core.timezones import format_berlin_time, format_berlin_time_short
from app.db.session import SessionLocal
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.site_admin_launch import SiteAdminLaunchService
from app.services.email_composer_settings import EmailComposerRuntimeSettings, EmailComposerSettingsService
from app.services.styling_settings import StylingRuntimeSettings, StylingSettingsService


def _shared_template_context(request: Request) -> dict[str, object]:
    user = getattr(request.state, "hub_user", None)
    return {
        "csrf_token": get_csrf_token(request),
        "can_launch_wordpress_admin": user is not None and user.role == "admin",
        "can_use_global_email_composer": user is not None and user.role == "admin",
        "unread_email_count": _unread_email_count(user),
        "email_composer_settings": _email_composer_settings(),
        "styling": _styling_settings(),
        "agent_page_context": _agent_page_context(request),
    }


def _agent_page_context(request: Request) -> dict[str, str] | None:
    """Describe the current Hub page so opening the agent can add it as context."""
    path = request.url.path.rstrip("/") or "/"
    if path.startswith("/agent"):
        return None

    direct_resource_patterns = (
        (r"^/customers/\d+/contacts/(\d+)$", "contact"),
        (r"^/contacts/(\d+)$", "contact"),
        (r"^/customers/\d+/cases/(\d+)(?:/.*)?$", "case"),
        (r"^/emails/cases/(\d+)(?:/.*)?$", "case"),
        (r"^/cases/(\d+)$", "case"),
        (r"^/sites/(\d+)$", "site"),
        (r"^/customers/(\d+)$", "customer"),
    )
    for pattern, resource_type in direct_resource_patterns:
        match = re.fullmatch(pattern, path)
        if match is not None:
            return {"type": resource_type, "key": match.group(1)}

    if path == "/calendar":
        week = request.query_params.get("week", "").strip()
        return {"type": "calendar", "key": week or "current"}
    if path == "/emails":
        selected = request.query_params.get("selected", "").strip()
        if re.fullmatch(r"(?:linked-\d+-\d+|unassigned-\d+)", selected):
            return {"type": "email", "key": selected}
        folder = request.query_params.get("folder", "inbox").strip()[:64]
        return {"type": "mailbox", "key": folder or "inbox"}

    page_contexts = (
        ("/", "Dashboard"),
        ("/sites", "Sites"),
        ("/customers", "Customers"),
        ("/contacts", "Kontakte"),
        ("/cases", "Fälle"),
        ("/email-templates", "E-Mail-Vorlagen"),
        ("/users", "Users"),
        ("/backups", "Backups"),
        ("/updates", "Update workbench"),
        ("/plugin-installations", "Plugin installation"),
        ("/assistant", "Assistant"),
        ("/account", "Account"),
        ("/styling", "Styling"),
    )
    for prefix, label in page_contexts:
        if path == prefix or (prefix != "/" and path.startswith(prefix + "/")):
            return {"type": "page", "key": label}
    return None


def _styling_settings() -> StylingRuntimeSettings:
    """Keep templates available during startup even before the singleton table exists."""
    try:
        with SessionLocal() as db:
            return StylingSettingsService(db=db).get_runtime_settings()
    except Exception:
        return StylingRuntimeSettings()


def _email_composer_settings() -> EmailComposerRuntimeSettings:
    """Let all composer variants share the persisted defaults without route plumbing."""
    try:
        with SessionLocal() as db:
            return EmailComposerSettingsService(db=db).get_runtime_settings()
    except Exception:
        return EmailComposerRuntimeSettings()


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
        CustomerZohoEmail.mailbox_state == "active",
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
                HubMailboxEmail.mailbox_state == "active",
            )
        )
        or 0
    )
    return linked_count + local_linked_count + unassigned_count


def create_templates(*, directory: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=directory, context_processors=[_shared_template_context])
    templates.env.filters["berlin_time"] = format_berlin_time
    templates.env.filters["berlin_time_short"] = format_berlin_time_short
    templates.env.globals["bridge_supports_admin_launch"] = SiteAdminLaunchService.bridge_supports_launch
    return templates
