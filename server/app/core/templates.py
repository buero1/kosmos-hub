import re
from jinja2 import pass_context
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from starlette.requests import Request
from starlette.templating import Jinja2Templates
from sqlalchemy import func, select

from app.core.csrf import get_csrf_token
from app.core.timezones import format_berlin_time, format_berlin_time_local, format_berlin_time_short
from app.db.session import SessionLocal
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.site_admin_launch import SiteAdminLaunchService
from app.services.email_composer_settings import EmailComposerRuntimeSettings, EmailComposerSettingsService
from app.services.email_ai_prompt_presets import DEFAULT_EMAIL_AI_PROMPT_PRESETS, EmailAiPromptPresetService
from app.services.hub_access_control import ACCESS_ACTIONS, ACCESS_MODULES, HubAccessControlService, can_open_settings
from app.services.hub_activity_catalog import activity_fields
from app.services.styling_settings import StylingRuntimeSettings, StylingSettingsService


def _shared_template_context(request: Request) -> dict[str, object]:
    if request.url.path in {"/account/login", "/account/setup"}:
        return {"csrf_token": get_csrf_token(request), "styling": _styling_settings()}
    user = getattr(request.state, "hub_user", None)
    module_access = _module_access(user)
    return {
        "csrf_token": get_csrf_token(request),
        "hub_module_access": module_access,
        "can_open_settings": can_open_settings(user, can_view=bool(module_access.get("settings", {}).get("view"))),
        "can_launch_wordpress_admin": bool(module_access.get("websites", {}).get("manage")),
        "can_use_global_email_composer": bool(module_access.get("emails", {}).get("create")),
        "unread_email_count": _unread_email_count(user) if module_access.get("emails", {}).get("view") else 0,
        "email_composer_settings": _email_composer_settings(),
        "email_ai_prompt_presets": _email_ai_prompt_presets(),
        "styling": _styling_settings(),
        "agent_page_context": _agent_page_context(request),
    }


def _module_access(user) -> dict[str, dict[str, object]]:
    if user is None:
        return {}
    if user.role == "admin":
        return {
            module.key: {"scope": "all", **{action: True for action in ACCESS_ACTIONS}}
            for module in ACCESS_MODULES
        }
    try:
        with SessionLocal() as db:
            return HubAccessControlService(db=db).module_access(user)
    except Exception:
        return {}


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
        (r"^/leads/(\d+)$", "lead"),
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
        ("/calls", "Anrufe"),
        ("/tasks", "Aufgaben"),
        ("/meetings", "Meetings"),
        ("/cases", "Fälle"),
        ("/email-templates", "E-Mail-Vorlagen"),
        ("/users", "Users"),
        ("/backups", "Backups"),
        ("/updates", "Update workbench"),
        ("/plugin-installations", "Plugin installation"),
        ("/assistant", "Assistant"),
        ("/account", "Account"),
        ("/settings", "Einstellungen"),
        ("/styling", "Einstellungen"),
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


def _email_ai_prompt_presets():
    """Make enabled email AI shortcuts available in every composer variant."""
    try:
        with SessionLocal() as db:
            return EmailAiPromptPresetService(db=db).list_presets(enabled_only=True)
    except Exception:
        return DEFAULT_EMAIL_AI_PROMPT_PRESETS


def _unread_email_count(user) -> int:
    if user is None:
        return 0
    try:
        with SessionLocal() as db:
            if user.role != "admin":
                from app.services.hub_mailbox import HubMailboxService
                from app.core.security import get_secret_cipher
                return HubMailboxService(db=db, cipher=get_secret_cipher(), actor=user.username,
                    public_base_url="").get_unread_count()
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


def format_finance_decimal(value: object, places: int = 2) -> str:
    """Render editable Finance decimals in the German notation used by the Hub."""
    try:
        decimal_value = Decimal(str(value or "").strip().replace(" ", "").replace(",", "."))
        step = Decimal("1").scaleb(-max(0, int(places)))
        return format(decimal_value.quantize(step, rounding=ROUND_HALF_UP), "f").replace(".", ",")
    except (InvalidOperation, ValueError):
        return str(value or "").replace(".", ",")


@pass_context
def _hub_link_visible(context, owner, module, identifier):
    from sqlalchemy.orm import object_session
    from app.models.base import Base
    request = context.get("request")
    user = getattr(request.state, "hub_user", None) if request else None
    user = user or context.get("user")
    if user is None or not identifier:
        return False
    if user.role == "admin":
        return True
    db = object_session(owner)
    if db is None:
        return False
    access = HubAccessControlService(db=db)
    if module in {"customers", "leads"}:
        return access.can_access_record(user=user, module_key=module, record_id=identifier)
    permission_module = "finance" if module.startswith("finance-") else module
    if not access.can(user, permission_module, "view"):
        return False
    table = {"contacts": "customer_contacts"}.get(module, "hub_" + module.replace("-", "_"))
    model = next((mapper.class_ for mapper in Base.registry.mappers if mapper.local_table.name == table), None)
    record = db.get(model, identifier) if model else None
    if module == "contacts":
        return access.can_access_contact(user=user, contact=record)
    if permission_module == "finance":
        if record is None:
            return False
        return all(access.can_access_record(user=user, module_key=target, record_id=value)
                   for target, value in (("customers", getattr(record, "customer_id", None)),
                                         ("leads", getattr(record, "lead_id", None))) if value)
    return True


def create_templates(*, directory: str) -> Jinja2Templates:
    from app.services.hub_record_info import actor_label, record_info
    from app.services.hub_document_template_catalog import NAME_FIELD, NAME_MIN_LENGTH
    from app.services.hub_note_catalog import note_fields
    templates = Jinja2Templates(directory=directory, context_processors=[_shared_template_context])
    templates.env.filters["berlin_time"] = format_berlin_time
    templates.env.filters["berlin_time_local"] = format_berlin_time_local
    templates.env.filters["berlin_time_short"] = format_berlin_time_short
    templates.env.filters["finance_decimal"] = format_finance_decimal
    templates.env.globals["hub_record_info"] = record_info
    templates.env.globals["hub_actor_label"] = actor_label
    templates.env.globals["hub_link_visible"] = _hub_link_visible
    templates.env.globals["bridge_supports_admin_launch"] = SiteAdminLaunchService.bridge_supports_launch
    templates.env.globals["document_template_name_field"] = NAME_FIELD
    templates.env.globals["document_template_name_min_length"] = NAME_MIN_LENGTH
    templates.env.globals["activity_field_catalog"] = lambda kind: {field.name: field for field in activity_fields(kind)}
    templates.env.globals["note_field_catalog"] = lambda creating=False: {field.name: field for field in note_fields(creating=creating)}
    return templates
