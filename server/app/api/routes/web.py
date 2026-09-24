from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.csrf import get_csrf_token, require_csrf
from app.core.security import get_secret_cipher
from app.core.templates import create_templates
from app.db.session import SessionLocal, get_db
from app.repositories.site_repository import SiteRepository
from app.schemas.dashboard import DashboardSummary
from app.services.fleet_inventory import FleetInventoryService
from app.services.audit import write_audit_log
from app.services.site_inventory import SiteInventoryService
from app.services.site_backups import SiteBackupService
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService
from app.services.site_users import SiteUserService
from app.services.user_deletion_batches import UserDeletionBatchService
from app.services.site_admin_launch import SiteAdminLaunchService
from app.services.maintenance_runs import MaintenanceRunService
from app.services.maintenance_worker import (
    schedule_pending_complete_site_updates,
    schedule_pending_direct_updates,
    schedule_pending_user_deletions,
)
from app.services.fleet_refresh import FleetRefreshService
from app.services.customer_directory import (
    CONTACT_FIELDS_LAYOUT_KEY,
    CUSTOMER_FIELDS_LAYOUT_KEY,
    HUB_CUSTOMER_FIELD_KEYS,
    CustomerDirectoryService,
)
from app.services.customer_communications import (
    CustomerCommunicationAttachmentUpload,
    CustomerCommunicationEmailTemplate,
    CustomerCommunicationImageError,
    CustomerCommunicationService,
)
from app.services.email_compose_images import EmailComposeImageError, EmailComposeImageService
from app.services.email_ai_rewrite import EmailAiRewriteError, EmailAiRewriteService
from app.services.email_template_folders import EmailTemplateFolderError, EmailTemplateFolderService
from app.services.customer_activities import (
    CALL_DIRECTION_OPTIONS,
    CALL_DURATION_OPTIONS,
    CALL_REMINDER_CHANNEL_OPTIONS,
    CALL_REMINDER_OPTIONS,
    CALL_STATUS_OPTIONS,
    CALL_TIME_OPTIONS,
    CustomerActivityError,
    CustomerActivityService,
    suggested_call_start,
)
from app.services.bavarian_holidays import bavarian_public_holidays
from app.services.hub_email_composition import compose_context, render_template
from app.services.hub_email_readers import (mailbox_status_data, mailbox_accounts, template_library, compose_options as email_compose_options,
    recipients as email_recipients, scheduled_context, download_attachment as download_shared_email_attachment, mailbox_case_context)
from app.services.hub_calendar import calendar_activities
from app.services.hub_mailbox_access import HubMailboxAccess
from app.services.hub_mailbox import HubMailboxService, MAILBOX_FOLDERS
from app.services.hub_mailbox_health import HubMailboxHealthService
from app.services.hub_mailbox_imap_sync import HubMailboxImapSyncService
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.models.hub_scheduled_email import HubScheduledEmail
from app.services.hub_mailbox_transport import DEFAULT_HUB_MAILBOX_SENDER_EMAIL
from app.services.task_email_reminder_worker import TaskEmailReminderWorker
from app.services.scheduled_emails import ScheduledEmailService
from app.services.scheduled_email_worker import ScheduledEmailWorker
from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerMeetingActivity, CustomerTaskActivity
from app.models.hub_finance_documents import HubFinanceDunning
from app.services.site_selection import SELECTABLE_CUSTOMER_STATUSES, build_site_selector_context
from app.services.styling_settings import FONT_FAMILY_OPTIONS, StylingSettingsError, StylingSettingsService
from app.services.module_layouts import ModuleLayoutError, ModuleLayoutService
from app.services.module_layout_catalog import ModuleLayoutDefinition, get_module_layout_definition
from app.services.plugin_installation_packages import PluginInstallationPackageService, PluginPackageError
from app.services.zoho_crm import ZOHO_RELEVANT_ACCOUNT_STATUSES, ZohoCrmError, ZohoCrmService
from app.services.zoho_contact_field_catalog import contact_fields
from app.services.hub_case_field_catalog import HUB_CASE_FIELDS
from app.services.hub_cases import CASE_FIELDS_LAYOUT_KEY, HubCaseEmailSource, HubCaseError, HubCaseService
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS, HUB_LEAD_SUBFORMS
from app.services.hub_leads import LEAD_FIELDS_LAYOUT_KEY, HubLeadError, HubLeadService
from app.services.hub_lead_emails import HubLeadEmailService
from app.services.hub_lead_notes import HubLeadNoteService
from app.services.hub_access_control import HubAccessControlService, permission_target
from app.services.hub_finance import (
    ARTICLE_FIELDS_LAYOUT_KEY,
    OFFER_FIELDS_LAYOUT_KEY,
    HubFinanceError,
    HubFinanceService,
)
from app.services.hub_crm_readers import HubCrmReadService
from app.services.hub_operation_layouts import layout_view
from app.services.hub_finance_pdf_readers import generated_status as finance_pdf_status, load_pdf as load_finance_pdf
from app.services.hub_operations import HubArtifact, HubOperationError, HubOperationService
from app.services.hub_administration import HubAdministrationService
from app.services.hub_operation_websites import website_items, website_site, website_inventory, website_sites
from app.services.wordpress_remote_catalog import execute_ui_remote
from app.services.wordpress_readers import plugin_catalog, users_inventory, deletion_batch
from app.services.wordpress_status import _fleet_refresh_status_payload, _direct_update_batch_status_payload, _complete_site_update_status_payload
from app.services.wordpress_workbench import dashboard_data, update_workbench, user_entries, backup_workbench, maintenance_batch, complete_run, fleet_run, fleet_history, fleet_status, batch_status, complete_status, is_removable_empty_test_registration
from app.models.hub_lead import HubLead
from app.services.hub_operation_records import form_input as record_form_input, customer_detail as read_customer_detail, lead_detail as read_lead_detail, customer_entries, lead_entries, customer_suggestions as read_customer_suggestions
from app.services.hub_customer_field_catalog import customer_create_fields, customer_create_defaults
from app.services.hub_activity_catalog import ACTIVITY_KINDS, activity_fields, activity_form_defaults
from app.services.hub_activity_responsibility import ActivityResponsibility, ACTIVITY_VIEWS
from app.services.hub_finance_field_catalog import ARTICLE_FIELDS, OFFER_FIELDS
from app.services.hub_finance_operations_shared import (
    finance_detail, finance_entries, finance_invoice_page, finance_options,
    form_input as finance_form_input,
)
from app.services.hub_finance_documents import (
    DUNNING_MODULE,
    FINANCE_DOCUMENT_MODULES,
    INVOICE_MODULE,
    ORDER_MODULE,
    RECURRING_INVOICE_MODULE,
    HubFinanceDocumentError,
    HubFinanceDocumentService,
)
from app.services.hub_finance_position_presets import (
    INVOICE_POSITION_PRESET_LIBRARY,
    POSITION_PRESET_LIBRARIES,
    SALES_POSITION_PRESET_LIBRARY,
    FinancePositionPresetView,
    HubFinancePositionPresetService,
)
from app.services.hub_finance_pdf_generation import (
    HubFinancePdfError,
    HubFinancePdfService,
    run_finance_pdf_generation,
)
from app.services.hub_invoice_email_batches import (
    HubInvoiceEmailBatchService,
    InvoiceEmailBatchError,
    run_invoice_email_batch,
)
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.template_placeholders import EMAIL_TEMPLATE_CONTEXTS, email_placeholders
from app.services.hub_workflows import CASE_COMPLETION_EMAIL_TEMPLATE_ID
from app.services.zoho_case_import import ZohoCaseImportService

templates = create_templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(include_in_schema=False)


def _email_gateway(request, db):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username)


def _mailbox_account_id(request):
    raw = request.query_params.get("account_id", "")
    if not raw:
        return None
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10 or int(raw) < 1:
        raise HTTPException(status_code=422, detail="Ungueltige Postfachauswahl.")
    return int(raw)


def _web_mailbox(request, db):
    try:
        return HubMailboxService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username,
                                 public_base_url=get_settings().public_base_url, account_id=_mailbox_account_id(request))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Das Postfach ist nicht verfuegbar.") from exc

_GERMAN_WEEKDAY_NAMES = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_GERMAN_MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Annotated[Session, Depends(get_db)]):
    data = dashboard_data(_website_gateway(request, db))
    summary = DashboardSummary.model_validate(data["summary"])
    latest_sites = data["sites"]
    inventory_summary = data["inventory_summary"]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "summary": summary,
            "inventory_summary": inventory_summary,
            "sites": latest_sites,
        },
    )


@router.get("/search/suggestions", response_class=JSONResponse)
def global_search_suggestions(request: Request, db: Annotated[Session, Depends(get_db)], q: str = ""):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if len(q.strip()) < 2:
        return JSONResponse({"groups": []}, headers={"Cache-Control": "private, no-store"})
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).query("hub.search", {"query": q.strip()[:80]})
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    groups = result["groups"]
    return JSONResponse({"groups": groups}, headers={"Cache-Control": "private, no-store"})


@router.get("/styling", response_class=HTMLResponse)
def styling_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_hub_admin(request)
    return RedirectResponse(url="/settings#account-styling", status_code=303)


@router.post("/styling")
def update_styling(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    font_family_key: Annotated[str, Form()] = "serif",
    background_color: Annotated[str, Form()] = "#f5f1e8",
    background_secondary_color: Annotated[str, Form()] = "#efe8da",
    panel_color: Annotated[str, Form()] = "#fffaf2",
    ink_color: Annotated[str, Form()] = "#1d2a2f",
    muted_color: Annotated[str, Form()] = "#6c7469",
    accent_color: Annotated[str, Form()] = "#0e7c66",
    accent_soft_color: Annotated[str, Form()] = "#d7efe8",
    border_color: Annotated[str, Form()] = "#d9d2c5",
    base_spacing: Annotated[int, Form()] = 16,
    panel_radius: Annotated[int, Form()] = 18,
    control_v1_height: Annotated[int, Form()] = 38,
    control_v1_font_size: Annotated[int, Form()] = 13,
    control_v1_padding: Annotated[int, Form()] = 12,
    control_v1_radius: Annotated[int, Form()] = 5,
    control_v2_height: Annotated[int, Form()] = 30,
    control_v2_font_size: Annotated[int, Form()] = 12,
    control_v2_padding: Annotated[int, Form()] = 10,
    control_v2_radius: Annotated[int, Form()] = 3,
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    service = StylingSettingsService(db=db)
    try:
        HubAdministrationService(db=db, cipher=get_secret_cipher(), actor=user.username).configure("styling",
            font_family_key=font_family_key,
            background_color=background_color,
            background_secondary_color=background_secondary_color,
            panel_color=panel_color,
            ink_color=ink_color,
            muted_color=muted_color,
            accent_color=accent_color,
            accent_soft_color=accent_soft_color,
            border_color=border_color,
            base_spacing=base_spacing,
            panel_radius=panel_radius,
            control_v1_height=control_v1_height,
            control_v1_font_size=control_v1_font_size,
            control_v1_padding=control_v1_padding,
            control_v1_radius=control_v1_radius,
            control_v2_height=control_v2_height,
            control_v2_font_size=control_v2_font_size,
            control_v2_padding=control_v2_padding,
            control_v2_radius=control_v2_radius,
        )
    except ValueError as exc:
        from app.api.routes.accounts import _account_context, _account_service, _require_persisted_current_user

        account_service = _account_service(db)
        account_user = _require_persisted_current_user(request, account_service)
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(
                request,
                account_user,
                account_service,
                error=str(exc),
                error_section="account-styling",
                page_mode="settings",
            ),
            status_code=400,
        )
    write_audit_log(db, site=None, actor=user.username, source="hub-styling", action="update-global-styling", result="success")
    db.commit()
    return RedirectResponse(url="/settings?styling=saved#account-styling", status_code=303)


@router.get("/sites", response_class=HTMLResponse)
def sites_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    plugin: str = "",
    status: str = "",
    customer_status: str = "",
    inventory: Literal["all", "present", "missing"] = "all",
    updates: Literal["all", "available", "wordpress", "plugins", "themes", "none", "missing"] = "all",
    update_plugin: str = "",
    wordpress: str = "",
    bridge: str = "",
):
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    try:
        all_items = website_items(_website_gateway(request, db), limit=1000)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    items = inventory_service.filter_items(
        all_items,
        query=q,
        plugin=plugin,
        status=status,
        customer_status=customer_status,
        inventory_state=inventory,
        updates_state=updates,
        update_plugin=update_plugin,
        wordpress_version=wordpress,
        bridge_version=bridge,
    )
    items.sort(key=lambda item: (item.site.domain.casefold(), item.site.id))
    return templates.TemplateResponse(
        request,
        "sites.html",
        {
            "items": items,
            "can_launch_wordpress_admin": getattr(request.state, "hub_user", None) is not None
            and request.state.hub_user.role == "admin",
            "csrf_token": get_csrf_token(request),
            "inventory_summary": inventory_service.summarize(all_items),
            "filters": {
                "q": q,
                "plugin": plugin,
                "status": status,
                "customer_status": customer_status,
                "inventory": inventory,
                "updates": updates,
                "update_plugin": update_plugin,
                "wordpress": wordpress,
                "bridge": bridge,
            },
            "filter_options": {
                "statuses": sorted({item.site.status for item in all_items}),
                "customer_statuses": sorted(
                    {
                        item.site.customer.zoho_status
                        for item in all_items
                        if item.site.customer is not None and item.site.customer.zoho_status
                    }
                ),
                "wordpress_versions": sorted(
                    {item.site.wordpress_version for item in all_items if item.site.wordpress_version},
                    reverse=True,
                ),
                "bridge_versions": sorted(
                    {item.site.bridge_version for item in all_items if item.site.bridge_version},
                    reverse=True,
                ),
            },
        },
    )


@router.post("/sites/{site_id}/open-wordpress-admin")
def open_site_wordpress_admin(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    destination: Annotated[Literal["dashboard", "plugins"], Form()] = "dashboard",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        launch = SiteAdminLaunchService(db=db, cipher=get_secret_cipher()).open_admin(
            site_id=site_id,
            actor=user.username,
            destination=destination,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return RedirectResponse(url=launch.launch_url, status_code=303)


@router.get("/customers", response_class=HTMLResponse)
def customers_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    status: str = "all",
    industry: str = "all",
    email: str = "all",
):
    valid_statuses = {*ZOHO_RELEVANT_ACCOUNT_STATUSES, "all"}
    if status not in valid_statuses:
        raise HTTPException(status_code=422, detail="Unknown customer status filter.")
    if email not in {"all", "unread"}:
        raise HTTPException(status_code=422, detail="Unknown customer email filter.")
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    access = HubAccessControlService(db=db)
    allowed_customer_ids = access.accessible_record_ids(user=user, module_key="customers")
    service = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    industry_options = service.list_industries(allowed_customer_ids=allowed_customer_ids)
    try:
        entries = customer_entries(HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username),
            query=q, status=None if status == "all" else status,
            industry=None if industry == "all" else industry, unread_email_only=email == "unread")
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    candidate_count = sum(entry.exact_match_candidate is not None for entry in entries)
    return templates.TemplateResponse(
        request,
        "customers.html",
        {
            "entries": entries,
            "candidate_count": candidate_count,
            "can_create_customer": access.can(user, "customers", "create"),
            "filters": {"q": q, "status": status, "industry": industry, "email": email},
            "status_options": ZOHO_RELEVANT_ACCOUNT_STATUSES,
            "industry_options": industry_options,
            "csrf_token": get_csrf_token(request),
        },
    )


def _ordered_layout_fields(db: Session, *, layout_key: str, fields: tuple[object, ...]) -> tuple[object, ...]:
    fields_by_key = {field.key: field for field in fields}
    ordered_keys = ModuleLayoutService(db=db).ordered_keys(
        layout_key=layout_key,
        default_keys=tuple(fields_by_key),
    )
    return tuple(fields_by_key[key] for key in ordered_keys)


def _module_layout_or_404(layout_key: str) -> ModuleLayoutDefinition:
    definition = get_module_layout_definition(layout_key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Module layout not found.")
    return definition


def _module_layout_context(
    request: Request,
    db: Session,
    *,
    definition: ModuleLayoutDefinition,
    saved: bool = False,
    error: str = "",
) -> dict[str, object]:
    try:
        view = layout_view(HubOperationService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username), definition.layout_key)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "definition": definition,
        "fields": view.fields,
        "show_more_index": view.show_more_index,
        "layout_revision": view.revision,
        "saved": saved,
        "error": error,
        "csrf_token": get_csrf_token(request),
    }


def _save_module_layout(request, db, layout_key, form):
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).execute(
        "layouts.update", {"layout_key": layout_key, "order_json": str(form.get("order_json") or ""),
            "expected_revision": str(form.get("expected_revision") or "")})


@router.get("/module-layouts/{layout_key}", response_class=HTMLResponse)
def module_layout_page(
    layout_key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    saved: bool = False,
):
    _require_hub_admin(request)
    definition = _module_layout_or_404(layout_key)
    return templates.TemplateResponse(
        request,
        "module_layout_edit.html",
        _module_layout_context(request, db, definition=definition, saved=saved),
    )


@router.post("/module-layouts/{layout_key}")
async def update_module_layout(
    layout_key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    definition = _module_layout_or_404(layout_key)
    try:
        _save_module_layout(request, db, definition.layout_key, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "module_layout_edit.html",
            _module_layout_context(request, db, definition=definition, error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action=f"update-{definition.layout_key}-layout",
        result="ok",
        detail=f"Updated global module layout {definition.layout_key}.",
    )
    db.commit()
    return RedirectResponse(url=f"/module-layouts/{definition.layout_key}?saved=true", status_code=303)


def _hub_customer_create_field_order(db: Session) -> tuple[str, ...]:
    layout_keys = (*HUB_CUSTOMER_FIELD_KEYS, "hub_postal_city")
    ordered_keys = ModuleLayoutService(db=db).ordered_keys(
        layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
        default_keys=layout_keys,
    )
    result: list[str] = []
    allowed_keys = set(HUB_CUSTOMER_FIELD_KEYS)
    for key in ordered_keys:
        expanded_keys = ("billing_postal_code", "billing_city") if key == "hub_postal_city" else (key,)
        for expanded_key in expanded_keys:
            if expanded_key in allowed_keys and expanded_key not in result:
                result.append(expanded_key)
    return tuple(result)


def _hub_customer_create_context(
    request: Request,
    db: Session,
    *,
    values: dict[str, str] | None = None,
    error: str = "",
) -> dict[str, object]:
    return {
        "csrf_token": get_csrf_token(request),
        "status_options": ZOHO_RELEVANT_ACCOUNT_STATUSES,
        "field_order": _hub_customer_create_field_order(db),
        "create_fields": {field.key: field for field in customer_create_fields()},
        "values": {**customer_create_defaults(), **(values or {})},
        "error": error,
    }


@router.get("/customers/new", response_class=HTMLResponse)
def new_customer_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_hub_admin(request)
    return templates.TemplateResponse(request, "customer_create.html", _hub_customer_create_context(request, db))


@router.post("/customers")
async def create_customer_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): value for key, value in form.items()
        if isinstance(value, str) and str(key).startswith("customer_field__")
    }
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            "customers.create", record_form_input(submitted_values, kind="customer"))
        customer = db.get(Customer, result.record_id)
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "customer_create.html",
            _hub_customer_create_context(request, db, values=submitted_values, error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-hub-customer",
        result="ok",
        detail=f"Created Hub Customer {customer.id}; customer data is not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(url=f"/customers/{customer.id}", status_code=303)


@router.get("/customers/suggestions", response_class=JSONResponse)
def customer_suggestions(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    status: str = "all",
    industry: str = "all",
    email: str = "all",
):
    """Return compact customer matches for the directory's type-ahead search."""
    valid_statuses = {*ZOHO_RELEVANT_ACCOUNT_STATUSES, "all"}
    if status not in valid_statuses:
        raise HTTPException(status_code=422, detail="Unknown customer status filter.")
    if email not in {"all", "unread"}:
        raise HTTPException(status_code=422, detail="Unknown customer email filter.")

    query = q.strip()
    if len(query) < 2:
        return {"suggestions": []}

    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        return read_customer_suggestions(HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username),
            query=query, status=status, industry=industry, email=email)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    created: int | None = None,
    deleted: bool = False,
    sync: str = "",
    sync_message: str = "",
):
    user = _require_hub_admin(request)
    service = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    access = HubAccessControlService(db=db)
    return templates.TemplateResponse(
        request,
        "contacts.html",
        {
            "entries": HubCrmReadService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).contact_entries(),
            "created": created,
            "deleted": deleted,
            "sync_state": sync if sync in {"success", "error"} else "",
            "sync_message": sync_message[:500] if sync in {"success", "error"} else "",
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/leads", response_class=HTMLResponse)
def leads_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    created: bool = False,
    deleted: bool = False,
):
    user = _require_hub_admin(request)
    access = HubAccessControlService(db=db)
    try:
        entries = lead_entries(HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username))
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            "entries": entries,
            "created": created,
            "deleted": deleted,
            "can_create_lead": access.can(user, "leads", "create"),
            "can_manage_lead_layout": user.role == "admin",
        },
    )


@router.get("/leads/new", response_class=HTMLResponse)
def new_lead_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_hub_admin(request)
    return templates.TemplateResponse(request, "lead_create.html", _lead_create_context(request, db))


@router.post("/leads")
async def create_lead_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = _lead_submitted_values(form)
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            "leads.create", record_form_input(submitted_values, kind="lead"))
        lead = db.get(HubLead, result.record_id)
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "lead_create.html",
            _lead_create_context(request, db, submitted_values=submitted_values, error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-hub-lead",
        result="ok",
        detail=f"Created Hub Lead {lead.id}; lead data is not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(url=f"/leads/{lead.id}", status_code=303)


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail_page(
    lead_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    fields: str = "",
    fields_message: str = "",
    layout: str = "",
    layout_message: str = "",
    activity: str = "",
    activity_message: str = "",
    note: str = "",
    note_message: str = "",
):
    user = _require_hub_admin(request)
    access = HubAccessControlService(db=db)
    can_view_lead_emails = access.can(user, "emails", "view")
    can_view_lead_finance = access.can(user, "finance", "view")
    can_view_lead_activities = access.can(user, "activities", "view")
    try:
        detail = read_lead_detail(HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username), {"lead_id": str(lead_id)})
    except HubOperationError:
        raise HTTPException(status_code=404, detail="Lead not found.")
    lead_email = next((field.value for field in detail.fields if field.key == "email"), "")
    finance_service = HubFinanceService(db=db, cipher=get_secret_cipher())
    activity_service = CustomerActivityService(db=db)
    berlin_now = datetime.now(ZoneInfo("Europe/Berlin"))
    call_start = suggested_call_start(berlin_now).replace(tzinfo=None)
    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            "detail": detail,
            "can_edit_lead": access.can_access_record(
                user=user, module_key="leads", record_id=lead_id, action="edit"
            ),
            "can_delete_lead": access.can_access_record(
                user=user, module_key="leads", record_id=lead_id, action="delete"
            ),
            "fields_state": fields if fields in {"success", "error"} else "",
            "fields_message": fields_message[:500],
            "layout_state": layout if layout in {"success", "error"} else "",
            "layout_message": layout_message[:500],
            "lead_email": lead_email,
            "lead_emails": (
                HubLeadEmailService(db=db, cipher=get_secret_cipher()).list_email_views(lead_id=lead_id, actor=user.username)
                if can_view_lead_emails else ()
            ),
            "lead_notes": HubLeadNoteService(db=db, cipher=get_secret_cipher()).list_note_views(lead_id=lead_id),
            "lead_finance_offers": finance_service.list_lead_offers(lead_id=lead_id) if can_view_lead_finance else (),
            "can_view_lead_emails": can_view_lead_emails,
            "can_create_lead_email": can_view_lead_emails and access.can(user, "emails", "create"),
            "can_view_lead_finance": can_view_lead_finance,
            "can_create_lead_finance": can_view_lead_finance and access.can(user, "finance", "create"),
            "can_view_lead_activities": can_view_lead_activities,
            "can_create_lead_activities": can_view_lead_activities and access.can(user, "activities", "create"),
            "can_edit_lead_activities": can_view_lead_activities and access.can(user, "activities", "edit"),
            "can_delete_lead_activities": can_view_lead_activities and access.can(user, "activities", "delete"),
            "note_state": note if note in {"success", "error"} else "",
            "note_message": note_message[:500],
            "activity_calls": ActivityResponsibility(db, user).filter_views("call", activity_service.list_calls(lead_id=lead_id, include_completed=True)) if can_view_lead_activities else (),
            "activity_tasks": ActivityResponsibility(db, user).filter_views("task", activity_service.list_tasks(lead_id=lead_id)) if can_view_lead_activities else (),
            "activity_meetings": ActivityResponsibility(db, user).filter_views("meeting", activity_service.list_meetings(lead_id=lead_id)) if can_view_lead_activities else (),
            **ActivityResponsibility(db, user).ui_context(),
            "activity_state": activity if activity in {"success", "error"} else "",
            "activity_message": activity_message[:500],
            "call_status_options": CALL_STATUS_OPTIONS,
            "call_direction_options": CALL_DIRECTION_OPTIONS,
            "call_duration_options": CALL_DURATION_OPTIONS,
            "call_time_options": CALL_TIME_OPTIONS,
            "call_reminder_channel_options": CALL_REMINDER_CHANNEL_OPTIONS,
            "call_reminder_options": CALL_REMINDER_OPTIONS,
            **activity_form_defaults(now=berlin_now, start=call_start),
            "csrf_token": get_csrf_token(request),
        },
    )


def _execute_case_operation(db: Session, actor: str, action: str, **values):
    encoded = {key: str(value) if value is not None else "" for key, value in values.items()}
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=actor).execute(f"cases.{action}", encoded)


def _case_email_source(db: Session, user, source_email_key: str):
    from app.services.hub_operation_cases import accessible_source
    service = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username)
    return accessible_source(service, user, HubAccessControlService(db=db), source_email_key)


def _execute_note_operation(db: Session, actor: str, module: str, action: str, **values):
    encoded = {key: str(value) if value is not None else "" for key, value in values.items()}
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=actor).execute(f"{module}.notes.{action}", encoded)


def _lead_note_redirect(lead_id: int, state: str, message: str) -> RedirectResponse:
    query = urlencode({"note": state, "note_message": message[:500]})
    return RedirectResponse(url=f"/leads/{lead_id}?{query}#lead-notes", status_code=303)


@router.post("/leads/{lead_id}/notes")
def create_lead_note(
    lead_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    title: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        note = _execute_note_operation(db, user.username, "leads", "create",
            lead_id=lead_id,
            title=title,
            content=content,
        )
    except ValueError as exc:
        db.rollback()
        return _lead_note_redirect(lead_id, "error", str(exc))
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-hub-lead-note",
        result="ok",
        detail=f"Created note {note.record_id} for lead {lead_id}; note content is not retained in the audit log.",
    )
    db.commit()
    return _lead_note_redirect(lead_id, "success", "Notiz wurde im Hub gespeichert.")


@router.post("/leads/{lead_id}/notes/{note_id}")
def update_lead_note(
    lead_id: int,
    note_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    title: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        note = _execute_note_operation(db, user.username, "leads", "update",
            lead_id=lead_id,
            note_id=note_id,
            title=title,
            content=content,
        )
    except ValueError as exc:
        db.rollback()
        return _lead_note_redirect(lead_id, "error", str(exc))
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-lead-note",
        result="ok",
        detail=f"Updated note {note.record_id} for lead {lead_id}; note content is not retained in the audit log.",
    )
    db.commit()
    return _lead_note_redirect(lead_id, "success", "Notiz wurde im Hub aktualisiert.")


@router.post("/leads/{lead_id}/notes/{note_id}/delete")
def delete_lead_note(
    lead_id: int,
    note_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        _execute_note_operation(db, user.username, "leads", "delete", lead_id=lead_id, note_id=note_id)
    except ValueError as exc:
        db.rollback()
        return _lead_note_redirect(lead_id, "error", str(exc))
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-lead-note",
        result="ok",
        detail=f"Deleted note {note_id} for lead {lead_id}.",
    )
    db.commit()
    return _lead_note_redirect(lead_id, "success", "Notiz wurde gelöscht.")


def _execute_activity_operation(db: Session, actor: str, kind: str, action: str, **values):
    if kind not in ACTIVITY_KINDS:
        raise HubOperationError("Die Aktivitätsart ist ungültig.")
    encoded = {
        key: ",".join(str(item) for item in value) if isinstance(value, (tuple, list)) else str(value) if value is not None else ""
        for key, value in values.items() if key != "assignee_user_id" or value is not None
    }
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=actor).execute(
        f"activities.{ACTIVITY_KINDS[kind][0]}.{action}", encoded,
    )


def _lead_activity_redirect(lead_id: int, state: str, message: str) -> RedirectResponse:
    query = urlencode({"activity": state, "activity_message": message})
    return RedirectResponse(url=f"/leads/{lead_id}?{query}#lead-activities", status_code=303)


def _lead_activity_fields(form, kind: str) -> dict[str, object]:
    singular = next((key for key, (plural, _label) in ACTIVITY_KINDS.items() if plural == kind), None)
    if singular is None:
        raise HubOperationError("Die Aktivitätsart ist ungültig.")
    return {
        field.name: [str(value) for value in form.getlist(field.name)] if field.multiple else str(form.get(field.name) or "")
        for field in activity_fields(singular)
        if field.name in form
    }

@router.post("/leads/{lead_id}/activities/{kind}")
async def create_lead_activity(lead_id: int, kind: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        fields = _lead_activity_fields(form, kind)
        singular = {"calls": "call", "tasks": "task", "meetings": "meeting"}.get(kind, "")
        activity = _execute_activity_operation(db, user.username, singular, "create",
            lead_id=lead_id, **fields)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _lead_activity_redirect(lead_id, "error", str(exc))
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action=f"create-lead-{kind}", result="ok", detail=f"Created {kind} activity {activity.record_id} for lead {lead_id}; description is not retained in the audit log.")
    db.commit()
    return _lead_activity_redirect(lead_id, "success", "Aktivität wurde angelegt.")


@router.post("/leads/{lead_id}/activities/{kind}/{activity_id}")
async def update_lead_activity(lead_id: int, kind: str, activity_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        fields = _lead_activity_fields(form, kind)
        singular = {"calls": "call", "tasks": "task", "meetings": "meeting"}.get(kind, "")
        activity = _execute_activity_operation(db, user.username, singular, "update",
            lead_id=lead_id, activity_id=activity_id, **fields)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _lead_activity_redirect(lead_id, "error", str(exc))
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action=f"update-lead-{kind}", result="ok", detail=f"Updated {kind} activity {activity.record_id} for lead {lead_id}; description is not retained in the audit log.")
    db.commit()
    return _lead_activity_redirect(lead_id, "success", "Aktivität wurde gespeichert.")


@router.post("/leads/{lead_id}/activities/{kind}/{activity_id}/delete")
async def delete_lead_activity(lead_id: int, kind: str, activity_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    singular = {"calls": "call", "tasks": "task", "meetings": "meeting"}.get(kind, "")
    try:
        _execute_activity_operation(db, user.username, singular, "delete", lead_id=lead_id, activity_id=activity_id)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _lead_activity_redirect(lead_id, "error", str(exc))
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action=f"delete-lead-{kind}", result="ok", detail=f"Deleted {kind} activity {activity_id} for lead {lead_id}.")
    db.commit()
    return _lead_activity_redirect(lead_id, "success", "Aktivität wurde gelöscht.")


@router.post("/leads/{lead_id}/activities/{kind}/{activity_id}/complete")
async def complete_lead_activity(lead_id: int, kind: str, activity_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    singular = {"calls": "call", "tasks": "task"}.get(kind, "")
    try:
        _execute_activity_operation(db, user.username, singular, "complete", lead_id=lead_id, activity_id=activity_id)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _lead_activity_redirect(lead_id, "error", str(exc))
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action=f"complete-lead-{kind}", result="ok", detail=f"Completed {kind} activity {activity_id} for lead {lead_id}.")
    db.commit()
    return _lead_activity_redirect(lead_id, "success", "Aktivität wurde abgeschlossen.")


@router.post("/leads/{lead_id}/fields")
async def update_lead_fields(lead_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            "leads.update", {**record_form_input(_lead_submitted_values(form), kind="lead"), "lead_id": str(lead_id)})
        lead = db.get(HubLead, result.record_id)
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/leads/{lead_id}?{query}", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-lead-fields",
        result="ok",
        detail=f"Updated Hub Lead {lead.id}; lead data is not retained in the audit log.",
    )
    db.commit()
    query = urlencode({"fields": "success", "fields_message": "Lead-Daten wurden im Hub gespeichert."})
    return RedirectResponse(url=f"/leads/{lead_id}?{query}#lead-fields", status_code=303)


@router.post("/leads/{lead_id}/layout")
async def update_lead_field_layout(lead_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    service = HubLeadService(db=db, cipher=get_secret_cipher())
    if service.get_detail(lead_id=lead_id) is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    try:
        _save_module_layout(request, db, LEAD_FIELDS_LAYOUT_KEY, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        query = urlencode({"layout": "error", "layout_message": str(exc)})
        return RedirectResponse(url=f"/leads/{lead_id}?{query}#lead-fields", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-lead-fields-layout",
        result="ok",
        detail="Updated the global lead field layout.",
    )
    db.commit()
    query = urlencode({"layout": "success", "layout_message": "Das globale Leadfelder-Layout wurde gespeichert."})
    return RedirectResponse(url=f"/leads/{lead_id}?{query}#lead-fields", status_code=303)


@router.post("/leads/{lead_id}/delete")
async def delete_lead_from_hub(lead_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute("leads.delete", {"lead_id": str(lead_id)})
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/leads/{lead_id}?{query}", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-lead",
        result="ok",
        detail=f"Deleted Hub Lead {lead_id}; no Zoho record was changed.",
    )
    db.commit()
    return RedirectResponse(url="/leads?deleted=true", status_code=303)


@router.get("/deletion-preview", response_class=JSONResponse)
def deletion_preview(request: Request, db: Annotated[Session, Depends(get_db)], target_path: str):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        return HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).query(
            "records.deletion_preview", {"target_path": target_path},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases", response_class=HTMLResponse)
def cases_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    created: bool = False,
    deleted: bool = False,
    sync: str = "",
    sync_message: str = "",
):
    _require_hub_admin(request)
    return templates.TemplateResponse(
        request,
        "cases.html",
        {
            "entries": HubCrmReadService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).case_entries(),
            "created": created,
            "deleted": deleted,
            "sync_state": sync if sync in {"success", "error"} else "",
            "sync_message": sync_message[:500] if sync in {"success", "error"} else "",
            "csrf_token": get_csrf_token(request),
        },
    )


@router.post("/cases/sync")
def synchronize_all_cases(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        result = ZohoCaseImportService(
            db=db,
            cipher=get_secret_cipher(),
            zoho_service=ZohoCrmService(
                db=db,
                cipher=get_secret_cipher(),
                public_base_url=get_settings().public_base_url,
            ),
        ).synchronize_all_cases()
    except ZohoCrmError as exc:
        db.rollback()
        query = urlencode({"sync": "error", "sync_message": str(exc)})
        return RedirectResponse(url=f"/cases?{query}", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="sync-all-zoho-cases",
        result="ok",
        detail=(
            f"Synchronized {result.synchronized_cases} Zoho Cases: "
            f"created {result.created_cases}, updated {result.updated_cases}, "
            f"unlinked {result.unlinked_cases}."
        ),
    )
    db.commit()
    message = (
        f"{result.synchronized_cases} Fälle aus Zoho aktualisiert: "
        f"{result.created_cases} neu, {result.updated_cases} aktualisiert"
    )
    if result.unlinked_cases:
        message += f", {result.unlinked_cases} ohne Kundenverknüpfung"
    if result.number_collisions:
        message += f", {result.number_collisions} mit Hub-Fallnummer"
    query = urlencode({"sync": "success", "sync_message": f"{message}."})
    return RedirectResponse(url=f"/cases?{query}", status_code=303)


@router.get("/cases/new", response_class=HTMLResponse)
def new_case_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    source_email_key: str = "",
    customer_id: int | None = None,
):
    _require_hub_admin(request)
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        source_email = service.source_email(source_email_key=source_email_key) if source_email_key else None
    except HubCaseError as exc:
        return templates.TemplateResponse(
            request,
            "case_create.html",
            _case_create_context(request, db, error=str(exc)),
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "case_create.html",
        _case_create_context(
            request,
            db,
            selected_customer_id=source_email.customer_id if source_email is not None else customer_id,
            source_email=source_email,
        ),
    )


@router.post("/cases")
async def create_case_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): str(value)
        for key, value in form.items()
        if isinstance(value, str) and str(key).startswith("case_field__")
    }
    raw_customer_id = str(form.get("customer_id") or "").strip()
    source_email_key = str(form.get("source_email_key") or "").strip()
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    source_email: HubCaseEmailSource | None = None
    try:
        source_email = _case_email_source(db, user, source_email_key) if source_email_key else None
        case = _execute_case_operation(db, user.username, "create",
            customer_id=raw_customer_id, source_email_key=source_email_key, **submitted_values)
    except (ValueError, HubCaseError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "case_create.html",
            _case_create_context(
                request,
                db,
                selected_customer_id=int(raw_customer_id) if raw_customer_id.isdigit() else None,
                submitted_values=submitted_values,
                source_email=source_email,
                error=str(exc),
            ),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-hub-case",
        result="ok",
        detail=(
            f"Created Hub Case {case.record_id} and linked one selected email; case data is not retained in the audit log."
            if source_email is not None
            else f"Created Hub Case {case.record_id}; case data is not retained in the audit log."
        ),
    )
    db.commit()
    if source_email is not None:
        query = urlencode({"email_link": "success", "email_link_message": "Die ausgewählte E-Mail wurde mit diesem Fall verknüpft."})
        return RedirectResponse(url=f"/cases/{case.record_id}?{query}#case-emails", status_code=303)
    return RedirectResponse(url=f"/cases/{case.record_id}", status_code=303)


@router.post("/cases/email-links")
async def link_customer_email_to_case(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    raw_case_id = str(form.get("case_id") or "").strip()
    source_email_key = str(form.get("source_email_key") or "").strip()
    try:
        case_id = int(raw_case_id)
        service = HubCaseService(db=db, cipher=get_secret_cipher())
        source_email = service.source_email(source_email_key=source_email_key)
        if source_email.customer_id is None:
            raise HubCaseError("Diese E-Mail kann nur über die E-Mail-Zentrale einem Fall zugeordnet werden.")
        _execute_case_operation(db, user.username, "link_email", case_id=case_id, source_email_key=source_email.key)
    except (ValueError, HubCaseError) as exc:
        db.rollback()
        if source_email_key.startswith("linked-"):
            try:
                customer_id = int(source_email_key.removeprefix("linked-").split("-", 1)[0])
            except ValueError:
                customer_id = 0
            if customer_id:
                return _customer_communication_redirect(customer_id, "error", str(exc))
        return RedirectResponse(url="/emails?case_link=error", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="link-email-to-hub-case",
        result="ok",
        detail=f"Linked one selected customer email to Hub Case {case_id}.",
    )
    db.commit()
    return _customer_communication_redirect(
        source_email.customer_id,
        "success",
        "Die E-Mail wurde dem ausgewählten Fall zugeordnet.",
    )


@router.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail_page(
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    fields: str = "",
    fields_message: str = "",
    layout: str = "",
    layout_message: str = "",
    email_link: str = "",
    email_link_message: str = "",
    completion_email: bool = False,
):
    _require_hub_admin(request)
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        detail = HubCrmReadService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).case_detail(case_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Case not found.")
    return templates.TemplateResponse(
        request,
        "case_detail.html",
        _case_detail_context(
            request,
            service=service,
            detail=detail,
            fields=fields,
            fields_message=fields_message,
            layout=layout,
            layout_message=layout_message,
            email_link=email_link,
            email_link_message=email_link_message,
            completion_email_template_id=(
                CASE_COMPLETION_EMAIL_TEMPLATE_ID
                if completion_email and HubCaseService.is_completed_status(detail.status)
                else ""
            ),
            completion_email_customer_id=detail.case.customer_id if completion_email else None,
        ),
    )


@router.post("/cases/{case_id}/email-links/{link_id}/delete")
async def unlink_email_from_case(
    case_id: int,
    link_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        _execute_case_operation(db, user.username, "unlink_email", case_id=case_id, link_id=link_id)
    except ValueError as exc:
        db.rollback()
        query = urlencode({"email_link": "error", "email_link_message": str(exc)})
        return RedirectResponse(url=f"/cases/{case_id}?{query}#case-emails", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="unlink-email-from-hub-case",
        result="ok",
        detail=f"Removed one email link from Hub Case {case_id}.",
    )
    db.commit()
    query = urlencode({"email_link": "success", "email_link_message": "Die E-Mail-Verknüpfung wurde gelöst."})
    return RedirectResponse(url=f"/cases/{case_id}?{query}#case-emails", status_code=303)


@router.post("/cases/{case_id}/fields")
async def update_case_fields(
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): str(value)
        for key, value in form.items()
        if isinstance(value, str) and str(key).startswith("case_field__")
    }
    raw_customer_id = str(form.get("customer_id") or "").strip()
    try:
        case = _execute_case_operation(db, user.username, "update",
            case_id=case_id, customer_id=raw_customer_id, **submitted_values)
        was_completed_now = case.outputs["completed_now"] == "true"
    except (ValueError, HubCaseError) as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/cases/{case_id}?{query}", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-case-fields",
        result="ok",
        detail=f"Updated Hub Case {case.record_id}; case data is not retained in the audit log.",
    )
    db.commit()
    query = {
        "fields": "success",
        "fields_message": "Falldaten wurden im Hub gespeichert.",
    }
    if was_completed_now:
        query["completion_email"] = "true"
    query = urlencode(query)
    return RedirectResponse(url=f"/cases/{case_id}?{query}", status_code=303)


@router.post("/cases/{case_id}/layout")
async def update_case_field_layout(
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    detail = service.get_detail(case_id=case_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Case not found.")
    try:
        _save_module_layout(request, db, CASE_FIELDS_LAYOUT_KEY, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        query = urlencode({"layout": "error", "layout_message": str(exc)})
        return RedirectResponse(url=f"/cases/{case_id}?{query}#case-fields", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-case-fields-layout",
        result="ok",
        detail="Updated the global case field layout.",
    )
    db.commit()
    query = urlencode({"layout": "success", "layout_message": "Das globale Fallfelder-Layout wurde gespeichert."})
    return RedirectResponse(url=f"/cases/{case_id}?{query}#case-fields", status_code=303)


@router.post("/cases/{case_id}/delete")
async def delete_case_from_hub(
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        case = _execute_case_operation(db, user.username, "delete", case_id=case_id)
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/cases/{case_id}?{query}", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-case",
        result="ok",
        detail=f"Deleted Hub Case {case.record_id}; no Zoho record was changed.",
    )
    db.commit()
    return RedirectResponse(url="/cases?deleted=true", status_code=303)


@router.get("/finance")
def finance_page(request: Request):
    _require_hub_admin(request)
    return RedirectResponse(url="/finance/articles", status_code=303)


@router.get("/finance/articles", response_class=HTMLResponse)
def finance_articles_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    created: bool = False,
    deleted: bool = False,
):
    _require_hub_admin(request)
    return templates.TemplateResponse(
        request,
        "finance_articles.html",
        {
            "entries": finance_entries(_finance_gateway(request, db), "articles"),
            "created": created,
            "deleted": deleted,
        },
    )


@router.get("/finance/articles/new", response_class=HTMLResponse)
def new_finance_article_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_hub_admin(request)
    return templates.TemplateResponse(request, "finance_article_create.html", _finance_article_create_context(request, db))


@router.post("/finance/articles")
async def create_finance_article_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = _finance_submitted_values(form, prefix="article_field__")
    try:
        article = _finance_gateway(request, db).execute("finance.articles.create", finance_form_input(form, kind="articles"))
    except (ValueError, HubFinanceError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "finance_article_create.html",
            _finance_article_create_context(request, db, submitted_values=submitted_values, error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-finance-article",
        result="ok",
        detail=f"Created Finance Article {article.record_id}; article data is not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(url=f"/finance/articles/{article.record_id}", status_code=303)


@router.get("/finance/articles/{article_id}", response_class=HTMLResponse)
def finance_article_detail_page(
    article_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    fields: str = "",
    fields_message: str = "",
    layout: str = "",
    layout_message: str = "",
):
    _require_hub_admin(request)
    service = HubFinanceService(db=db, cipher=get_secret_cipher())
    detail = _finance_read(request, db, "articles", article_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Article not found.")
    return templates.TemplateResponse(
        request,
        "finance_article_detail.html",
        _finance_article_detail_context(
            request,
            detail=detail,
            fields=fields,
            fields_message=fields_message,
            layout=layout,
            layout_message=layout_message,
        ),
    )


@router.post("/finance/articles/{article_id}/fields")
async def update_finance_article_fields(article_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        article = _finance_gateway(request, db).execute("finance.articles.update", finance_form_input(form, kind="articles", record_id=article_id))
    except (ValueError, HubFinanceError) as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/finance/articles/{article_id}?{query}", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-finance-article",
        result="ok",
        detail=f"Updated Finance Article {article.record_id}; article data is not retained in the audit log.",
    )
    db.commit()
    query = urlencode({"fields": "success", "fields_message": "Artikeldaten wurden im Hub gespeichert."})
    return RedirectResponse(url=f"/finance/articles/{article_id}?{query}#finance-article-fields", status_code=303)


@router.post("/finance/articles/{article_id}/layout")
async def update_finance_article_layout(article_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    if _finance_read(request, db, "articles", article_id) is None:
        raise HTTPException(status_code=404, detail="Article not found.")
    try:
        _save_module_layout(request, db, ARTICLE_FIELDS_LAYOUT_KEY, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        query = urlencode({"layout": "error", "layout_message": str(exc)})
        return RedirectResponse(url=f"/finance/articles/{article_id}?{query}#finance-article-fields", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action="update-finance-article-layout", result="ok", detail="Updated the global finance article field layout.")
    db.commit()
    query = urlencode({"layout": "success", "layout_message": "Das globale Artikelfelder-Layout wurde gespeichert."})
    return RedirectResponse(url=f"/finance/articles/{article_id}?{query}#finance-article-fields", status_code=303)


@router.post("/finance/articles/{article_id}/delete")
async def delete_finance_article(article_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        article = _finance_gateway(request, db).execute("finance.articles.delete", {"record_id": str(article_id)})
    except (ValueError, HubFinanceError) as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/finance/articles/{article_id}?{query}", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action="delete-finance-article", result="ok", detail=f"Deleted Finance Article {article.record_id}; no Zoho Books record was changed.")
    db.commit()
    return RedirectResponse(url="/finance/articles?deleted=true", status_code=303)


@router.get("/finance/offers", response_class=HTMLResponse)
def finance_offers_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    created: bool = False,
    deleted: bool = False,
):
    _require_hub_admin(request)
    return templates.TemplateResponse(
        request,
        "finance_offers.html",
        {
            "entries": finance_entries(_finance_gateway(request, db), "offers"),
            "created": created,
            "deleted": deleted,
        },
    )


@router.get("/finance/offers/new", response_class=HTMLResponse)
def new_finance_offer_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    customer_id: int | None = None,
    lead_id: int | None = None,
):
    _require_hub_admin(request)
    return templates.TemplateResponse(
        request,
        "finance_offer_create.html",
        _finance_offer_create_context(
            request,
            db,
            selected_customer_id=customer_id,
            selected_lead_id=lead_id,
        ),
    )


@router.post("/finance/offers")
async def create_finance_offer_page(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = _finance_submitted_values(form, prefix="offer_")
    customer_id = _optional_form_id(form.get("customer_id"))
    lead_id = _optional_form_id(form.get("lead_id"))
    contact_id = _optional_form_id(form.get("contact_id"))
    pdf_template_id = _optional_form_id(form.get("pdf_template_id"))
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            "finance.offers.create",
            finance_form_input(form, kind="offers"),
        )
    except (ValueError, HubFinanceError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "finance_offer_create.html",
            _finance_offer_create_context(
                request,
                db,
                selected_customer_id=customer_id,
                selected_lead_id=lead_id,
                selected_contact_id=contact_id,
                selected_pdf_template_id=pdf_template_id,
                submitted_values=submitted_values,
                error=str(exc),
            ),
            status_code=400,
        )
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action="create-finance-offer", result="ok", detail=f"Created Finance Offer {result.record_id}; offer data is not retained in the audit log.")
    db.commit()
    background_tasks.add_task(run_finance_pdf_generation, result.background_token)
    return RedirectResponse(url=result.href, status_code=303)


@router.get("/finance/offers/{offer_id}", response_class=HTMLResponse)
def finance_offer_detail_page(
    offer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    fields: str = "",
    fields_message: str = "",
    layout: str = "",
    layout_message: str = "",
):
    _require_hub_admin(request)
    service = HubFinanceService(db=db, cipher=get_secret_cipher())
    detail = _finance_read(request, db, "offers", offer_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Offer not found.")
    return templates.TemplateResponse(
        request,
        "finance_offer_detail.html",
        _finance_offer_detail_context(
            request,
            service=service,
            detail=detail,
            fields=fields,
            fields_message=fields_message,
            layout=layout,
            layout_message=layout_message,
        ),
    )


@router.post("/finance/offers/{offer_id}/fields")
async def update_finance_offer_fields(
    offer_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        offer = _finance_gateway(request, db).execute("finance.offers.update", finance_form_input(form, kind="offers", record_id=offer_id))
        generation_token = offer.background_token
    except (ValueError, HubFinanceError) as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/finance/offers/{offer_id}?{query}", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action="update-finance-offer", result="ok", detail=f"Updated Finance Offer {offer.record_id}; offer data is not retained in the audit log.")
    db.commit()
    background_tasks.add_task(run_finance_pdf_generation, generation_token)
    query = urlencode({"fields": "success", "fields_message": "Angebotsdaten wurden im Hub gespeichert."})
    return RedirectResponse(url=f"/finance/offers/{offer_id}?{query}#finance-offer-fields", status_code=303)


@router.post("/finance/offers/{offer_id}/layout")
async def update_finance_offer_layout(offer_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    if _finance_read(request, db, "offers", offer_id) is None:
        raise HTTPException(status_code=404, detail="Offer not found.")
    try:
        _save_module_layout(request, db, OFFER_FIELDS_LAYOUT_KEY, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        query = urlencode({"layout": "error", "layout_message": str(exc)})
        return RedirectResponse(url=f"/finance/offers/{offer_id}?{query}#finance-offer-fields", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action="update-finance-offer-layout", result="ok", detail="Updated the global finance offer field layout.")
    db.commit()
    query = urlencode({"layout": "success", "layout_message": "Das globale Angebotsfelder-Layout wurde gespeichert."})
    return RedirectResponse(url=f"/finance/offers/{offer_id}?{query}#finance-offer-fields", status_code=303)


@router.post("/finance/offers/{offer_id}/duplicate")
async def duplicate_finance_offer(offer_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        result = _finance_gateway(request, db).execute("finance.offers.duplicate", {"record_id": str(offer_id)})
    except (ValueError, HubFinanceError) as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/finance/offers/{offer_id}?{query}", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action="duplicate-finance-offer", result="ok",
                    detail=f"Duplicated Finance Offer {offer_id} as {result.record_id}; no offer content is retained in the audit log.")
    db.commit()
    return RedirectResponse(url=result.href, status_code=303)


@router.post("/finance/offers/{offer_id}/discard-copy")
async def discard_finance_offer_copy(offer_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        result = _finance_gateway(request, db).execute("finance.offers.discard_copy", {"record_id": str(offer_id)})
    except (ValueError, HubFinanceError) as exc:
        db.rollback()
        query = urlencode({"edit": "true", "fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/finance/offers/{offer_id}?{query}", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action="discard-finance-offer-copy", result="ok",
                    detail=f"Discarded unfinished Finance Offer copy {offer_id}.")
    db.commit()
    return RedirectResponse(url=result.href, status_code=303)


@router.post("/finance/offers/{offer_id}/delete")
async def delete_finance_offer(offer_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        offer = _finance_gateway(request, db).execute("finance.offers.delete", {"record_id": str(offer_id)})
    except (ValueError, HubFinanceError) as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/finance/offers/{offer_id}?{query}", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action="delete-finance-offer", result="ok", detail=f"Deleted Finance Offer {offer.record_id}; no Zoho Books record was changed.")
    db.commit()
    return RedirectResponse(url="/finance/offers?deleted=true", status_code=303)


@router.post("/finance/position-presets", response_class=JSONResponse)
async def create_finance_position_preset(request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        from app.services.hub_operation_position_presets import get_preset
        operations = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username)
        result = operations.execute("finance.presets.create", {
            "library_key": str(form.get("library_key") or ""),
            "name": str(form.get("name") or ""),
            "lines_json": str(form.get("lines_json") or ""),
        })
        preset = get_preset(operations, result.record_id, result.outputs["library_key"])
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"error": str(exc)}, status_code=400)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-finance-position-preset",
        result="ok",
        detail=f"Created position preset {preset.id} in {preset.library_key} with {preset.line_count} lines; line data is not retained in the audit log.",
    )
    db.commit()
    return JSONResponse({"preset": _finance_position_preset_json(preset)}, status_code=201)


@router.post("/finance/position-presets/{preset_id}/delete", response_class=JSONResponse)
async def delete_finance_position_preset(preset_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    library_key = str(form.get("library_key") or "")
    try:
        preset = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            "finance.presets.delete", {"preset_id": str(preset_id), "library_key": library_key},
        )
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"error": str(exc)}, status_code=400)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-finance-position-preset",
        result="ok",
        detail=f"Deleted position preset {preset.record_id} from {library_key}; line data is not retained in the audit log.",
    )
    db.commit()
    return JSONResponse({"deleted": preset_id})


@router.post("/finance/invoices/email-review", response_class=JSONResponse)
async def review_invoice_emails(request: Request, db: Annotated[Session, Depends(get_db)]):
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Ungültige Anfrage."}, status_code=400)
    require_csrf(request, str(payload.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        review = HubInvoiceEmailBatchService(db=db, cipher=get_secret_cipher()).review(
            payload.get("ids"), actor=user.username,
        )
    except InvoiceEmailBatchError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(review, headers={"Cache-Control": "private, no-store"})


@router.post("/finance/invoices/email-confirm", response_class=JSONResponse)
async def confirm_invoice_emails(
    request: Request, background_tasks: BackgroundTasks, db: Annotated[Session, Depends(get_db)],
):
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Ungültige Anfrage."}, status_code=400)
    require_csrf(request, str(payload.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        batch = HubInvoiceEmailBatchService(db=db, cipher=get_secret_cipher()).confirm(
            token=str(payload.get("review_token") or ""), actor=user.username,
        )
    except InvoiceEmailBatchError as exc:
        db.rollback()
        return JSONResponse({"error": str(exc)}, status_code=409)
    write_audit_log(
        db, site=None, actor=user.username, source="hub-web", action="confirm-invoice-email-batch",
        result="ok", detail=f"Confirmed invoice email batch {batch.id}; recipients and content are not retained in the audit log.",
    )
    db.commit()
    if batch.status == "queued":
        background_tasks.add_task(run_invoice_email_batch, batch.id)
    return JSONResponse({"batch_id": batch.id}, headers={"Cache-Control": "private, no-store"})


@router.get("/finance/invoices/email-batches/{batch_id}", response_class=JSONResponse)
def invoice_email_batch_status(batch_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    user = _require_hub_admin(request)
    try:
        status = HubInvoiceEmailBatchService(db=db, cipher=get_secret_cipher()).status(
            batch_id=batch_id, actor=user.username,
        )
    except InvoiceEmailBatchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(status, headers={"Cache-Control": "private, no-store"})


@router.get("/finance/{module_key}", response_class=HTMLResponse)
def finance_documents_page(
    module_key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    created: bool = False,
    deleted: bool = False,
    page: int = 1,
):
    _require_hub_admin(request)
    module = _finance_document_module(module_key)
    service = HubFinanceDocumentService(db=db, cipher=get_secret_cipher())
    invoice_page = finance_invoice_page(_finance_gateway(request, db), page) if module.is_invoice else None
    return templates.TemplateResponse(
        request,
        "finance_documents.html",
        {
            "module": module,
            "entries": invoice_page.entries if invoice_page else finance_entries(_finance_gateway(request, db), module.key),
            "invoice_page": invoice_page,
            "invoice_page_links": range(max(1, invoice_page.page - 2), min(invoice_page.page_count, invoice_page.page + 2) + 1) if invoice_page else (),
            "created": created,
            "deleted": deleted,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/finance/{module_key}/new", response_class=HTMLResponse)
def new_finance_document_page(
    module_key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    customer_id: int | None = None,
    invoice_id: int | None = None,
):
    _require_hub_admin(request)
    module = _finance_document_module(module_key)
    try:
        context = _finance_document_create_context(
            request,
            db,
            module=module,
            selected_customer_id=customer_id,
            source_invoice_id=invoice_id,
        )
    except (ValueError, HubFinanceDocumentError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "finance_document_create.html",
        context,
    )


@router.post("/finance/{module_key}")
async def create_finance_document_page(
    module_key: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    module = _finance_document_module(module_key)
    submitted_values = _finance_submitted_values(form, prefix="document_")
    customer_id = _optional_form_id(form.get("customer_id"))
    contact_id = _optional_form_id(form.get("contact_id"))
    link_id = _optional_form_id(form.get("linked_record_id"))
    pdf_template_id = _optional_form_id(form.get("pdf_template_id"))
    try:
        document = _finance_gateway(request, db).execute(f"finance.{module.key}.create", finance_form_input(form, kind=module.key))
        generation_token = document.background_token
    except (ValueError, HubFinanceDocumentError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "finance_document_create.html",
            _finance_document_create_context(
                request,
                db,
                module=module,
                selected_customer_id=customer_id,
                selected_contact_id=contact_id,
                selected_link_id=link_id,
                selected_pdf_template_id=pdf_template_id,
                submitted_values=submitted_values,
                error=str(exc),
            ),
            status_code=400,
        )
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action=f"create-finance-{module.key}", result="ok", detail=f"Created Finance {module.singular} {document.record_id}; document data is not retained in the audit log.")
    db.commit()
    if generation_token:
        background_tasks.add_task(run_finance_pdf_generation, generation_token)
    return RedirectResponse(url=f"/finance/{module.key}/{document.record_id}", status_code=303)


@router.get("/finance/{module_key}/{document_id}", response_class=HTMLResponse)
def finance_document_detail_page(
    module_key: str,
    document_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    fields: str = "",
    fields_message: str = "",
    layout: str = "",
    layout_message: str = "",
    email: str = "",
    email_message: str = "",
):
    _require_hub_admin(request)
    module = _finance_document_module(module_key)
    service = HubFinanceDocumentService(db=db, cipher=get_secret_cipher())
    detail = _finance_read(request, db, module.key, document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Finance document not found.")
    return templates.TemplateResponse(
        request,
        "finance_document_detail.html",
        _finance_document_detail_context(
            request,
            service=service,
            module=module,
            detail=detail,
            fields=fields,
            fields_message=fields_message,
            layout=layout,
            layout_message=layout_message,
            email=email,
            email_message=email_message,
        ),
    )


@router.get("/finance/invoices/{invoice_id}/pdf")
def finance_invoice_pdf_preview(invoice_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_hub_admin(request)
    try:
        pdf, content = load_finance_pdf(_finance_gateway(request, db), "invoices", invoice_id, source="original")
    except (ValueError, HubFinanceDocumentError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type=pdf.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(pdf.filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/finance/orders/{order_id}/pdf")
def finance_order_pdf_preview(order_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_hub_admin(request)
    try:
        pdf, content = load_finance_pdf(_finance_gateway(request, db), "orders", order_id, source="original")
    except (ValueError, HubFinanceDocumentError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type=pdf.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(pdf.filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/finance/{document_type}/{document_id}/generated-pdf")
def finance_generated_pdf_preview(
    document_type: str,
    document_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    try:
        pdf, content = load_finance_pdf(_finance_gateway(request, db), document_type, document_id)
    except (ValueError, HubFinancePdfError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type=pdf.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(pdf.filename or 'beleg.pdf', safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/finance/{document_type}/{document_id}/generated-pdf/status")
def finance_generated_pdf_status(
    document_type: str,
    document_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    try:
        view = finance_pdf_status(_finance_gateway(request, db), document_type, document_id)
    except (ValueError, HubFinancePdfError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": view.status if view else "missing"}


@router.post("/finance/{document_type}/{document_id}/generated-pdf")
async def regenerate_finance_pdf(
    document_type: str,
    document_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        result = _finance_gateway(request, db).execute("finance.pdf.generate", {"kind": document_type, "record_id": str(document_id)})
        generation_token = result.background_token
    except (ValueError, HubFinancePdfError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action=f"generate-finance-{document_type}-pdf",
        result="queued",
        detail=f"Queued generated PDF for {document_type} {document_id}.",
    )
    db.commit()
    background_tasks.add_task(run_finance_pdf_generation, generation_token)
    return RedirectResponse(url=f"/finance/{document_type}/{document_id}", status_code=303)


@router.post("/finance/{module_key}/{document_id}/fields")
async def update_finance_document_fields(
    module_key: str,
    document_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    module = _finance_document_module(module_key)
    try:
        document = _finance_gateway(request, db).execute(f"finance.{module.key}.update", finance_form_input(form, kind=module.key, record_id=document_id))
        generation_token = document.background_token
    except (ValueError, HubFinanceDocumentError) as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/finance/{module.key}/{document_id}?{query}", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action=f"update-finance-{module.key}", result="ok", detail=f"Updated Finance {module.singular} {document.record_id}; document data is not retained in the audit log.")
    db.commit()
    if generation_token:
        background_tasks.add_task(run_finance_pdf_generation, generation_token)
    query = urlencode({"fields": "success", "fields_message": "Die Daten wurden im Hub gespeichert."})
    return RedirectResponse(url=f"/finance/{module.key}/{document_id}?{query}#finance-document-fields", status_code=303)


@router.post("/finance/{module_key}/{document_id}/layout")
async def update_finance_document_layout(module_key: str, document_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    module = _finance_document_module(module_key)
    service = HubFinanceDocumentService(db=db, cipher=get_secret_cipher())
    if _finance_read(request, db, module.key, document_id) is None:
        raise HTTPException(status_code=404, detail="Finance document not found.")
    try:
        _save_module_layout(request, db, module.layout_key, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        query = urlencode({"layout": "error", "layout_message": str(exc)})
        return RedirectResponse(url=f"/finance/{module.key}/{document_id}?{query}#finance-document-fields", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action=f"update-finance-{module.key}-layout", result="ok", detail=f"Updated the global Finance {module.singular} field layout.")
    db.commit()
    query = urlencode({"layout": "success", "layout_message": f"Das Feld-Layout für {module.singular.lower()} wurde gespeichert."})
    return RedirectResponse(url=f"/finance/{module.key}/{document_id}?{query}#finance-document-fields", status_code=303)


@router.post("/finance/{module_key}/{document_id}/delete")
async def delete_finance_document(module_key: str, document_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    module = _finance_document_module(module_key)
    try:
        document = _finance_gateway(request, db).execute(f"finance.{module.key}.delete", {"record_id": str(document_id)})
    except (ValueError, HubFinanceDocumentError) as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/finance/{module.key}/{document_id}?{query}", status_code=303)
    write_audit_log(db, site=None, actor=user.username, source="hub-web", action=f"delete-finance-{module.key}", result="ok", detail=f"Deleted Finance {module.singular} {document.record_id}; no Zoho Books record was changed.")
    db.commit()
    return RedirectResponse(url=f"/finance/{module.key}?deleted=true", status_code=303)


@router.get("/activities/{activity_kind}/{activity_id}", response_class=HTMLResponse)
def customer_activity_permalink(
    activity_kind: str,
    activity_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        activity = HubCrmReadService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).activity_record(activity_kind, activity_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if activity is None:
        raise HTTPException(status_code=404, detail="Activity not found.")
    if activity_kind == "task" and activity.status != "planned":
        return templates.TemplateResponse(
            request,
            "customer_activity_standalone.html",
            {"activity": activity},
        )
    if activity.customer_id is not None:
        return RedirectResponse(
            url=f"/customers/{activity.customer_id}#{activity_kind}-{activity.id}",
            status_code=302,
        )
    if activity.lead_id is not None:
        return RedirectResponse(url=f"/leads/{activity.lead_id}#{activity_kind}-{activity.id}", status_code=302)
    if activity_kind != "task":
        local_date = activity.starts_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin")).date()
        return RedirectResponse(
            url=f"/calendar?week={local_date.isoformat()}#{activity_kind}-{activity.id}",
            status_code=302,
        )
    return templates.TemplateResponse(
        request,
        "customer_activity_standalone.html",
        {"activity": activity},
    )


_ACTIVITY_DIRECTORY_PAGES = {
    "call": ("Anrufe", "Anruf neu anlegen", "calls"),
    "task": ("Aufgaben", "Aufgabe neu anlegen", "tasks"),
    "meeting": ("Meetings", "Meeting neu anlegen", "meetings"),
}


def _activity_directory_page(*, request: Request, db: Session, kind: str):
    user = _require_hub_admin(request)
    view = request.query_params.get("view", "mine")
    view = view if view in dict(ACTIVITY_VIEWS) else "mine"
    title, create_label, _ = _ACTIVITY_DIRECTORY_PAGES[kind]
    berlin_now = datetime.now(ZoneInfo("Europe/Berlin"))
    default_start = suggested_call_start(berlin_now).replace(tzinfo=None)
    return templates.TemplateResponse(
        request,
        "activity_directory.html",
        {
            "entries": HubCrmReadService(db=db, cipher=get_secret_cipher(), actor=user.username).activity_entries(kind, view=view),
            "activity_view": view,
            **ActivityResponsibility(db, user).ui_context(),
            "activity_kind": kind,
            "title": title,
            "create_label": create_label,
            "create_href": f"/calendar?create={kind}",
            "activity_state": request.query_params.get("activity", "")
            if request.query_params.get("activity") in {"success", "error"}
            else "",
            "activity_message": request.query_params.get("activity_message", "")[:500],
            "call_status_options": CALL_STATUS_OPTIONS,
            "call_duration_options": CALL_DURATION_OPTIONS,
            "call_time_options": CALL_TIME_OPTIONS,
            "call_reminder_channel_options": CALL_REMINDER_CHANNEL_OPTIONS,
            "call_reminder_options": CALL_REMINDER_OPTIONS,
            **activity_form_defaults(now=berlin_now, start=default_start),
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/calls", response_class=HTMLResponse)
def calls_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    return _activity_directory_page(request=request, db=db, kind="call")


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    return _activity_directory_page(request=request, db=db, kind="task")


@router.get("/meetings", response_class=HTMLResponse)
def meetings_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    return _activity_directory_page(request=request, db=db, kind="meeting")


def _activity_directory_redirect(kind: str, state: str, message: str) -> RedirectResponse:
    page = _ACTIVITY_DIRECTORY_PAGES.get(kind, ("", "", "tasks"))[2]
    query = urlencode({"activity": state, "activity_message": message})
    return RedirectResponse(url=f"/{page}?{query}", status_code=303)


@router.post("/activities/{activity_kind}/{activity_id}")
async def update_activity_from_directory(
    activity_kind: str,
    activity_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    plural_kind = _ACTIVITY_DIRECTORY_PAGES.get(activity_kind, ("", "", ""))[2]
    try:
        fields = _lead_activity_fields(form, plural_kind)
        activity = _execute_activity_operation(db, user.username, activity_kind, "update", activity_id=activity_id, **fields)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _activity_directory_redirect(activity_kind, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action=f"update-directory-{activity_kind}",
        result="ok",
        detail=f"Updated {activity_kind} activity {activity.record_id} from its directory; description is not retained in the audit log.",
    )
    db.commit()
    return _activity_directory_redirect(activity_kind, "success", "Aktivität wurde gespeichert.")


@router.post("/activities/{activity_kind}/{activity_id}/delete", response_class=JSONResponse)
def delete_activity_from_panel(
    activity_kind: str,
    activity_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    if activity_kind not in _ACTIVITY_DIRECTORY_PAGES:
        raise HTTPException(status_code=404, detail="Die Aktivität wurde nicht gefunden.")
    try:
        result = _execute_activity_operation(db, user.username, activity_kind, "delete", activity_id=activity_id)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return JSONResponse({"detail": str(exc)}, status_code=400)
    write_audit_log(
        db, site=None, actor=user.username, source="hub-web",
        action=f"delete-panel-{activity_kind}", result="ok",
        detail=f"Deleted {activity_kind} activity {result.record_id}; activity content is not retained in the audit log.",
    )
    db.commit()
    return JSONResponse({"ok": True, "message": "Aktivität wurde gelöscht."})


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    week: str = "",
    calendar: str = "",
    calendar_message: str = "",
):
    user = _require_hub_admin(request)
    policy = ActivityResponsibility(db, user)
    view = request.query_params.get("view", "mine")
    view = view if view in dict(ACTIVITY_VIEWS) else "mine"
    berlin_now = datetime.now(ZoneInfo("Europe/Berlin"))
    week_start = _calendar_week_start(week, today=berlin_now.date())
    week_end = week_start + timedelta(days=6)
    try:
        activities = calendar_activities(_email_gateway(request, db), week_start, view=view)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    holidays_by_date = {
        holiday_date: holiday_name
        for year in {week_start.year, week_end.year}
        for holiday_date, holiday_name in bavarian_public_holidays(year).items()
    }
    calendar_days = tuple(
        {
            "date": (day := week_start + timedelta(days=offset)).isoformat(),
            "label": _GERMAN_WEEKDAY_NAMES[offset],
            "short_label": _GERMAN_WEEKDAY_NAMES[offset][:2],
            "day": day.day,
            "holiday_name": holidays_by_date.get(day),
            "is_today": day == berlin_now.date(),
            "events": tuple(
                activity for activity in activities
                if activity.start_date == day.isoformat()
            ),
        }
        for offset in range(7)
    )
    default_start = suggested_call_start(berlin_now).replace(tzinfo=None)
    if not week_start <= default_start.date() <= week_end:
        default_start = datetime.combine(week_start, time(hour=9))
    can_manage_calendar = policy.right("view")
    access = HubAccessControlService(db=db)
    user = getattr(request.state, "hub_user", None)
    allowed_customers = access.accessible_record_ids(user=user, module_key="customers") if can_manage_calendar else set()
    allowed_leads = access.accessible_record_ids(user=user, module_key="leads") if can_manage_calendar else set()
    customers = tuple(
        db.scalars(
            select(Customer)
            .where(Customer.is_visible.is_(True))
            .where(Customer.id.in_(allowed_customers) if allowed_customers is not None else True)
            .order_by(Customer.name.asc(), Customer.id.asc())
        ).all()
    ) if can_manage_calendar else ()
    leads = HubLeadService(db=db, cipher=get_secret_cipher()).list_leads(allowed_lead_ids=allowed_leads) if can_manage_calendar else ()
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "calendar_days": calendar_days,
            "activity_view": view,
            **policy.ui_context(),
            "calendar_hours": tuple(f"{hour:02d}:00" for hour in range(24)),
            "calendar_time_slots": CALL_TIME_OPTIONS,
            "calendar_week_start": week_start.isoformat(),
            "calendar_week_number": week_start.isocalendar().week,
            "calendar_previous_week": (week_start - timedelta(days=7)).isoformat(),
            "calendar_next_week": (week_start + timedelta(days=7)).isoformat(),
            "calendar_today_week": (berlin_now.date() - timedelta(days=berlin_now.weekday())).isoformat(),
            "calendar_week_label": _calendar_week_label(week_start, week_end),
            "calendar_state": calendar if calendar in {"success", "error"} else "",
            "calendar_message": calendar_message[:500] if calendar in {"success", "error"} else "",
            "calendar_customers": customers,
            "calendar_leads": leads,
            "calendar_default_date": default_start.strftime("%Y-%m-%d"),
            "calendar_default_time": default_start.strftime("%H:%M"),
            "call_status_options": CALL_STATUS_OPTIONS,
            "call_duration_options": CALL_DURATION_OPTIONS,
            "call_time_options": CALL_TIME_OPTIONS,
            "call_reminder_channel_options": CALL_REMINDER_CHANNEL_OPTIONS,
            "call_reminder_options": CALL_REMINDER_OPTIONS,
            **activity_form_defaults(now=berlin_now, start=default_start),
            "can_manage_calendar": can_manage_calendar,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.post("/calendar/activities")
def schedule_calendar_activity(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    customer_id: Annotated[str, Form()] = "",
    lead_id: Annotated[str, Form()] = "",
    activity_kind: Annotated[str, Form()] = "meeting",
    name: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "planned",
    direction: Annotated[str, Form()] = "outbound",
    start_date: Annotated[str, Form()] = "",
    start_time: Annotated[str, Form()] = "",
    due_date: Annotated[str, Form()] = "",
    due_time: Annotated[str, Form()] = "",
    duration_minutes: Annotated[str, Form()] = "60",
    reminder_channels: Annotated[list[str] | None, Form()] = None,
    reminder_minutes_before: Annotated[list[str] | None, Form()] = None,
    reminder_channel: Annotated[str, Form()] = "email",
    task_reminder_minutes_before: Annotated[str, Form()] = "0",
    description: Annotated[str, Form()] = "",
    week: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    week_start = _calendar_week_start(week, today=datetime.now(ZoneInfo("Europe/Berlin")).date())
    try:
        selected_customer_id = int(customer_id) if customer_id.strip() else None
        selected_lead_id = int(lead_id) if lead_id.strip() else None
    except ValueError:
        return _calendar_redirect(week_start, "error", "Bitte einen gültigen Kunden oder Lead auswählen.")
    try:
        if activity_kind == "call":
            activity = _execute_activity_operation(db, user.username, "call", "create",
                assignee_user_id=assignee_user_id,
                customer_id=selected_customer_id,
                lead_id=selected_lead_id,
                name=name,
                status=status,
                direction=direction,
                start_date=start_date,
                start_time=start_time,
                duration_minutes=duration_minutes,
                reminder_channels=reminder_channels or [],
                reminder_minutes_before=reminder_minutes_before or [],
                description=description,
            )
            action = "schedule-calendar-call"
            message = "Anruf wurde im Kalender geplant."
        elif activity_kind == "task":
            activity = _execute_activity_operation(db, user.username, "task", "create",
                assignee_user_id=assignee_user_id,
                customer_id=selected_customer_id,
                lead_id=selected_lead_id,
                name=name,
                status=status,
                due_date=due_date,
                due_time=due_time,
                reminder_channel=reminder_channel,
                reminder_minutes_before=task_reminder_minutes_before,
                description=description,
            )
            action = "schedule-calendar-task"
            message = "Aufgabe wurde angelegt."
        elif activity_kind == "meeting":
            activity = _execute_activity_operation(db, user.username, "meeting", "create",
                assignee_user_id=assignee_user_id,
                customer_id=selected_customer_id,
                lead_id=selected_lead_id,
                name=name,
                status=status,
                start_date=start_date,
                start_time=start_time,
                duration_minutes=duration_minutes,
                reminder_channels=reminder_channels or [],
                reminder_minutes_before=reminder_minutes_before or [],
                description=description,
            )
            action = "schedule-calendar-meeting"
            message = "Meeting wurde im Kalender geplant."
        else:
            raise CustomerActivityError("Bitte eine gültige Terminart wählen.")
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _calendar_redirect(week_start, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action=action,
        result="ok",
        detail=f"Scheduled calendar {activity_kind} {activity.record_id} for customer {selected_customer_id or 'none'}, lead {selected_lead_id or 'none'}; description is not retained in the audit log.",
    )
    db.commit()
    if activity_kind == "task":
        if selected_customer_id is not None:
            return _customer_activity_redirect(selected_customer_id, "success", message)
        if selected_lead_id is not None:
            return RedirectResponse(url=f"/leads/{selected_lead_id}#lead-activities", status_code=303)
        return RedirectResponse(url=f"/activities/task/{activity.record_id}", status_code=303)
    return _calendar_redirect(week_start, "success", message)


@router.post("/calendar/activities/{activity_kind}/{activity_id}")
def update_calendar_activity(
    activity_kind: str,
    activity_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    customer_id: Annotated[str, Form()] = "",
    lead_id: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "planned",
    direction: Annotated[str, Form()] = "outbound",
    start_date: Annotated[str, Form()] = "",
    start_time: Annotated[str, Form()] = "",
    duration_minutes: Annotated[str, Form()] = "60",
    reminder_channels: Annotated[list[str] | None, Form()] = None,
    reminder_minutes_before: Annotated[list[str] | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    week: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    week_start = _calendar_week_start(week, today=datetime.now(ZoneInfo("Europe/Berlin")).date())
    try:
        selected_customer_id = int(customer_id) if customer_id.strip() else None
        selected_lead_id = int(lead_id) if lead_id.strip() else None
    except ValueError:
        return _calendar_redirect(week_start, "error", "Bitte einen gültigen Kunden oder Lead auswählen.")

    try:
        if activity_kind == "call":
            activity = _execute_activity_operation(db, user.username, "call", "update",
                assignee_user_id=assignee_user_id,
                activity_id=activity_id,
                new_customer_id=selected_customer_id,
                new_lead_id=selected_lead_id,
                name=name,
                status=status,
                direction=direction,
                start_date=start_date,
                start_time=start_time,
                duration_minutes=duration_minutes,
                reminder_channels=reminder_channels or [],
                reminder_minutes_before=reminder_minutes_before or [],
                description=description,
            )
            action = "update-calendar-call"
            message = "Anruf wurde im Kalender gespeichert."
        elif activity_kind == "meeting":
            activity = _execute_activity_operation(db, user.username, "meeting", "update",
                assignee_user_id=assignee_user_id,
                activity_id=activity_id,
                new_customer_id=selected_customer_id,
                new_lead_id=selected_lead_id,
                name=name,
                status=status,
                start_date=start_date,
                start_time=start_time,
                duration_minutes=duration_minutes,
                reminder_channels=reminder_channels or [],
                reminder_minutes_before=reminder_minutes_before or [],
                description=description,
            )
            action = "update-calendar-meeting"
            message = "Meeting wurde im Kalender gespeichert."
        else:
            raise CustomerActivityError("Bitte eine gültige Terminart wählen.")
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _calendar_redirect(week_start, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action=action,
        result="ok",
        detail=f"Updated calendar {activity_kind} {activity.record_id} for customer {selected_customer_id or 'none'}, lead {selected_lead_id or 'none'}; description is not retained in the audit log.",
    )
    db.commit()
    return _calendar_redirect(week_start, "success", message)


@router.post("/contacts/sync")
def synchronize_all_contacts(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        result = ZohoCrmService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).synchronize_all_contacts()
    except ZohoCrmError as exc:
        db.rollback()
        query = urlencode({"sync": "error", "sync_message": str(exc)})
        return RedirectResponse(url=f"/contacts?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="sync-all-zoho-contacts",
        result="ok",
        detail=(
            f"Synchronized {result.synchronized_contacts} Zoho Contacts: "
            f"created {result.created_contacts}, updated {result.updated_contacts}, "
            f"removed {result.removed_contacts}."
        ),
    )
    db.commit()
    query = urlencode(
        {
            "sync": "success",
            "sync_message": (
                f"{result.synchronized_contacts} Kontakte aus Zoho aktualisiert: "
                f"{result.created_contacts} neu, {result.updated_contacts} aktualisiert."
            ),
        }
    )
    return RedirectResponse(url=f"/contacts?{query}", status_code=303)


@router.get("/contacts/new", response_class=HTMLResponse)
def new_contact_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    customer_id: int | None = None,
):
    _require_hub_admin(request)
    return templates.TemplateResponse(
        request,
        "contact_create.html",
        _contact_create_context(request, db, selected_customer_id=customer_id),
    )


@router.post("/contacts")
async def create_contact_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    csrf_token = str(form.get("csrf_token") or "")
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    submitted_values = {
        key: str(value)
        for key, value in form.items()
        if isinstance(value, str) and key.startswith("contact_field__")
    }
    raw_customer_id = str(form.get("customer_id") or "").strip()
    try:
        customer_id = int(raw_customer_id) if raw_customer_id else None
    except ValueError:
        customer_id = None

    try:
        contact = _execute_contact_operation(
            db, user.username, "create", customer_id=raw_customer_id, **submitted_values,
        )
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "contact_create.html",
            _contact_create_context(
                request,
                db,
                selected_customer_id=customer_id,
                submitted_values=submitted_values,
                error=str(exc),
            ),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-hub-contact",
        result="ok",
        detail=f"Created Hub Contact {contact.record_id}; contact data is not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(url=f"/contacts/{contact.record_id}", status_code=303)


@router.get("/contacts/{contact_id}", response_class=HTMLResponse)
def contact_detail_page(
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    fields: str = "",
    fields_message: str = "",
    layout: str = "",
    layout_message: str = "",
):
    service = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    try:
        detail = HubCrmReadService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).contact_detail(contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Contact not found.")
    return templates.TemplateResponse(
        request,
        "customer_contact_detail.html",
        _contact_detail_context(
            request,
            db,
            detail=detail,
            fields=fields,
            fields_message=fields_message,
            layout=layout,
            layout_message=layout_message,
        ),
    )


@router.post("/contacts/{contact_id}/fields")
async def update_hub_contact_fields(
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): str(value)
        for key, value in form.items()
        if isinstance(value, str) and str(key).startswith("contact_field__")
    }
    try:
        contact = _execute_contact_operation(
            db, user.username, "update", contact_id=contact_id, **submitted_values,
        )
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/contacts/{contact_id}?{query}#contact-fields", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-contact-fields",
        result="ok",
        detail=f"Updated Contact {contact.record_id}; contact data is not retained in the audit log.",
    )
    db.commit()
    message = "Kontaktdaten wurden in Zoho CRM gespeichert." if contact.outputs.get("zoho_id") else "Kontaktdaten wurden im Hub gespeichert."
    query = urlencode({"fields": "success", "fields_message": message})
    return RedirectResponse(url=f"/contacts/{contact_id}?{query}#contact-fields", status_code=303)


@router.post("/contacts/{contact_id}/link")
async def update_hub_contact_link(
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    raw_customer_id = str(form.get("customer_id") or "").strip()
    try:
        contact = _execute_contact_operation(
            db, user.username, "link_customer", contact_id=contact_id, new_customer_id=raw_customer_id,
        )
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/contacts/{contact_id}?{query}", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-contact-link",
        result="ok",
        detail=f"Updated the Hub customer link for Contact {contact.record_id}.",
    )
    db.commit()
    query = urlencode({"fields": "success", "fields_message": "Kundenverknüpfung wurde im Hub gespeichert."})
    return RedirectResponse(url=f"/contacts/{contact_id}?{query}", status_code=303)


@router.post("/contacts/{contact_id}/layout")
async def update_hub_contact_field_layout(
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    detail = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).get_contact_detail_by_id(contact_id=contact_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Contact not found.")
    try:
        _save_module_layout(request, db, CONTACT_FIELDS_LAYOUT_KEY, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        query = urlencode({"layout": "error", "layout_message": str(exc)})
        return RedirectResponse(url=f"/contacts/{contact_id}?{query}#contact-fields", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-contact-fields-layout",
        result="ok",
        detail="Updated the global contact field layout.",
    )
    db.commit()
    query = urlencode({"layout": "success", "layout_message": "Das globale Kontaktfelder-Layout wurde gespeichert."})
    return RedirectResponse(url=f"/contacts/{contact_id}?{query}#contact-fields", status_code=303)


@router.post("/contacts/{contact_id}/delete")
async def delete_contact_from_hub(
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    try:
        contact = _execute_contact_operation(db, user.username, "delete", contact_id=contact_id)
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/contacts/{contact_id}?{query}", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-contact",
        result="ok",
        detail=f"Deleted Hub Contact {contact.record_id}; no Zoho record was changed.",
    )
    db.commit()
    return RedirectResponse(url=f"/contacts?{urlencode({'deleted': 'true'})}", status_code=303)


@router.get("/emails", response_class=HTMLResponse)
def mailbox_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    folder: str = "inbox",
    unread: bool = False,
    selected: str = "",
    email_state: str = "",
    email_message: str = "",
):
    _require_hub_admin(request)
    if folder not in MAILBOX_FOLDERS:
        raise HTTPException(status_code=422, detail="Unknown mailbox folder.")
    mailbox_service = _web_mailbox(request, db)
    accounts = mailbox_accounts(_email_gateway(request, db))
    account_id = mailbox_service.account_id
    if account_id is None and "account_id" not in request.query_params and len(accounts) == 1:
        account_id = accounts[0]["id"]
        mailbox_service = HubMailboxService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username,
                                            public_base_url=get_settings().public_base_url, account_id=account_id)
    visible_addresses = {row["email"] for row in accounts if account_id is None or row["id"] == account_id}
    mailbox = mailbox_service.get_view(folder=folder, unread_only=unread, selected_key=selected)
    mailbox_sync_failures = HubMailboxImapSyncService(
        db=db, cipher=get_secret_cipher(), public_base_url=get_settings().public_base_url,
    ).list_failed_messages()
    mailbox_sync_failures = tuple(row for row in mailbox_sync_failures if row.mailbox_email in visible_addresses)
    mailbox_sync_warnings = tuple(
        (email_address, HubMailboxHealthService._alert_reason(state=state, now=datetime.now(UTC)))
        for state, email_address in db.execute(
            select(HubMailboxImapSyncState, HubMailboxAccount.email_address)
            .join(HubMailboxAccount)
            .where(
                HubMailboxImapSyncState.folder == "INBOX",
                HubMailboxAccount.enabled.is_(True),
                HubMailboxAccount.verified_at.is_not(None),
                HubMailboxAccount.email_address.in_(visible_addresses),
            )
        ).all()
        if HubMailboxHealthService._alert_reason(state=state, now=datetime.now(UTC)) is not None
    )
    return templates.TemplateResponse(
        request,
        "emails.html",
        {
            "mailbox": mailbox,
            "mailbox_accounts": accounts,
            "mailbox_account_id": account_id,
            "mailbox_sync_failures": mailbox_sync_failures,
            "mailbox_sync_warnings": mailbox_sync_warnings,
            "linked_case": _mailbox_linked_case(db, mailbox.selected),
            "folder": folder,
            "unread": unread,
            "email_state": email_state if email_state in {"success", "error"} else "",
            "email_message": email_message[:500] if email_state in {"success", "error"} else "",
            "csrf_token": get_csrf_token(request),
        },
    )


_TRANSLATED_EMAIL_TEMPLATE_FOLDERS = (
    ("Kunden", "Kunden Hub"),
    ("Leads", "Leads Hub"),
)


def _translated_email_template_names(
    email_templates: tuple[CustomerCommunicationEmailTemplate, ...],
    *,
    destination_folder: str,
) -> frozenset[str]:
    def name_key(value: str) -> str:
        return " ".join(value.casefold().split())

    standard_template_ids = {
        item.id
        for item in email_templates
        if item.category.casefold() == destination_folder.casefold() and name_key(item.name) == "standard_hub"
    }
    return frozenset(
        name_key(item.name)
        for item in email_templates
        if item.category.casefold() == destination_folder.casefold()
        and item.cloned_from in standard_template_ids
        and name_key(item.name) != "standard_hub"
    )


@router.get("/email-templates", response_class=HTMLResponse)
def email_template_management_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    template: str = "",
    folder: str = "",
    search: str = "",
    scroll: int | None = None,
    focus: str = "",
    state: str = "",
    message: str = "",
):
    """Manage the Hub's locally stored email templates without querying Zoho."""
    _require_hub_admin(request)
    service = _customer_communication_service(db)
    email_templates = template_library(_email_gateway(request, db))
    translated_template_names = {
        source_folder.casefold(): _translated_email_template_names(
            email_templates,
            destination_folder=destination_folder,
        )
        for source_folder, destination_folder in _TRANSLATED_EMAIL_TEMPLATE_FOLDERS
    }
    template_library_templates = tuple(
        {
            "id": email_template.id,
            "name": email_template.name,
            "subject": email_template.subject,
            "module": email_template.module,
            "category": email_template.category or email_template.module or "Weitere Vorlagen",
            "compiler_mode": email_template.compiler_mode,
            "context_module": email_template.context_module,
            "is_translated": (
                " ".join(email_template.name.casefold().split())
                in translated_template_names.get(email_template.category.casefold(), frozenset())
            ),
            "is_reviewed": email_template.content_reviewed,
        }
        for email_template in email_templates
    )
    discovered_folders = tuple(dict.fromkeys(
        item["category"] for item in template_library_templates if item["category"]
    ))
    folder_service = EmailTemplateFolderService(db=db)
    if folder_service.ensure_folders(discovered_folders):
        db.commit()
    template_folders = folder_service.list_folders()
    selected_template = None
    selected_template_id = template.strip()
    selected_template_folder = ""
    if selected_template_id:
        try:
            selected_template = service.get_email_template_source(template_id=selected_template_id)
            selected_template_folder = next(
                (
                    item["category"]
                    for item in template_library_templates
                    if item["id"] == selected_template_id
                ),
                "Weitere Vorlagen",
            )
        except ValueError:
            selected_template_id = ""
    template_folder_options = tuple(item.name for item in template_folders)
    return_folder = folder.strip()[:255]
    return_search = search.strip()[:500]
    return_scroll = min(max(scroll, 0), 10_000_000) if scroll is not None else None
    return_focus = focus.strip()[:255]
    if selected_template is not None:
        return_folder = return_folder or selected_template_folder
        return_focus = return_focus or selected_template_id
    return_query: dict[str, str] = {}
    if return_folder:
        return_query["folder"] = return_folder
    if return_search:
        return_query["search"] = return_search
    if return_scroll is not None:
        return_query["scroll"] = str(return_scroll)
    if return_focus:
        return_query["focus"] = return_focus
    template_library_return_url = "/email-templates"
    if return_query:
        template_library_return_url += f"?{urlencode(return_query)}"
    return templates.TemplateResponse(
        request,
        "email_templates.html",
        {
            "template_library_templates": template_library_templates,
            "template_library_folders": tuple(
                {"id": item.id, "name": item.name} for item in template_folders
            ),
            "initial_template_folder": return_folder,
            "initial_template_search": return_search,
            "initial_template_scroll": return_scroll,
            "initial_template_focus": return_focus,
            "selected_template": selected_template,
            "template_placeholders": email_placeholders(),
            "email_template_contexts": EMAIL_TEMPLATE_CONTEXTS,
            "selected_template_id": selected_template_id,
            "selected_template_folder": selected_template_folder,
            "template_folder_options": template_folder_options,
            "template_library_return_url": template_library_return_url,
            "template_return_folder": return_folder,
            "template_return_search": return_search,
            "template_return_scroll": "" if return_scroll is None else str(return_scroll),
            "template_return_focus": return_focus,
            "template_state": state if state in {"success", "error"} else "",
            "template_message": message[:500] if state in {"success", "error"} else "",
            "csrf_token": get_csrf_token(request),
        },
    )


@router.post("/email-template-folders")
def create_email_template_folder(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        folder = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute("emails.templates.folders.create", {"name": name})
    except ValueError as exc:
        db.rollback()
        query = urlencode({"state": "error", "message": str(exc)})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-email-template-folder",
        result="ok",
        detail=f"Created email template folder {folder.record_id}.",
    )
    db.commit()
    query = urlencode({
        "folder": folder.outputs["folder_name"],
        "state": "success",
        "message": "Vorlagenordner wurde angelegt.",
    })
    return RedirectResponse(url=f"/email-templates?{query}", status_code=303)


@router.post("/email-template-folders/{folder_id}/delete")
def delete_email_template_folder(
    folder_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        folder = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute("emails.templates.folders.delete", {"folder_id": str(folder_id)})
        folder_name = folder.outputs["folder_name"]
    except ValueError as exc:
        db.rollback()
        query = urlencode({"state": "error", "message": str(exc)})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-email-template-folder",
        result="ok",
        detail=f"Deleted empty email template folder {folder_id} ({folder_name}).",
    )
    db.commit()
    query = urlencode({"state": "success", "message": "Leerer Vorlagenordner wurde gelöscht."})
    return RedirectResponse(url=f"/email-templates?{query}", status_code=303)


@router.post("/email-templates/bulk/move")
def move_email_templates(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    template_ids: Annotated[list[str] | None, Form()] = None,
    folder_name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute("emails.templates.move", {
            "template_ids": json.dumps(template_ids or []), "folder_name": folder_name,
        })
        destination = result.outputs["folder_name"]
        count = int(result.outputs["changed_count"])
    except (EmailTemplateFolderError, ValueError) as exc:
        db.rollback()
        query = urlencode({"state": "error", "message": str(exc)})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="move-hub-email-templates",
        result="ok",
        detail=f"Moved {count} local email templates to folder {destination}; template content is not retained in the audit log.",
    )
    db.commit()
    noun = "Vorlage wurde" if count == 1 else "Vorlagen wurden"
    query = urlencode({
        "folder": destination,
        "state": "success",
        "message": f"{count} {noun} in den Ordner „{destination}“ verschoben.",
    })
    return RedirectResponse(url=f"/email-templates?{query}", status_code=303)


@router.post("/email-templates/bulk/delete")
def delete_email_templates(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    template_ids: Annotated[list[str] | None, Form()] = None,
    return_folder: Annotated[str, Form()] = "",
    confirmation: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    if confirmation != "confirmed":
        query = urlencode({"folder": return_folder, "state": "error", "message": "Das Löschen wurde nicht bestätigt."})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute("emails.templates.delete", {"template_ids": json.dumps(template_ids or [])})
        count = int(result.outputs["changed_count"])
    except ValueError as exc:
        db.rollback()
        query = urlencode({"folder": return_folder, "state": "error", "message": str(exc)})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-email-templates",
        result="ok",
        detail=f"Deleted {count} local email templates; template content is not retained in the audit log.",
    )
    db.commit()
    noun = "Vorlage wurde" if count == 1 else "Vorlagen wurden"
    query = urlencode({
        "folder": return_folder,
        "state": "success",
        "message": f"{count} {noun} aus dem Hub gelöscht.",
    })
    return RedirectResponse(url=f"/email-templates?{query}", status_code=303)


def _email_template_return_state(
    *,
    folder: str = "",
    search: str = "",
    scroll: str = "",
    focus: str = "",
) -> dict[str, str]:
    values: dict[str, str] = {}
    normalized_folder = folder.strip()[:255]
    normalized_search = search.strip()[:500]
    normalized_focus = focus.strip()[:255]
    try:
        normalized_scroll = min(max(int(scroll), 0), 10_000_000) if scroll.strip() else None
    except ValueError:
        normalized_scroll = None
    if normalized_folder:
        values["folder"] = normalized_folder
    if normalized_search:
        values["search"] = normalized_search
    if normalized_scroll is not None:
        values["scroll"] = str(normalized_scroll)
    if normalized_focus:
        values["focus"] = normalized_focus
    return values


@router.post("/email-templates/{template_id}")
def update_email_template(
    template_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    context_module: Annotated[str, Form()] = "",
    folder_name: Annotated[str | None, Form()] = None,
    return_folder: Annotated[str, Form()] = "",
    return_search: Annotated[str, Form()] = "",
    return_scroll: Annotated[str, Form()] = "",
    return_focus: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        template = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute("emails.templates.update", {
            "template_id": template_id, "name": name, "subject": subject, "content": content,
            "context_module": context_module, **({"folder_name": folder_name} if folder_name is not None else {}),
        })
    except ValueError as exc:
        db.rollback()
        query_values = {
            "template": template_id,
            "state": "error",
            "message": str(exc),
            **_email_template_return_state(
                folder=return_folder,
                search=return_search,
                scroll=return_scroll,
                focus=return_focus,
            ),
        }
        query = urlencode(query_values)
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-email-template",
        result="ok",
        detail=f"Updated local email template {template.outputs['template_id']}; the template content is not retained in the audit log.",
    )
    db.commit()
    query_values = {
        "template": template.outputs['template_id'],
        "state": "success",
        "message": "Vorlage wurde im Hub gespeichert.",
        **_email_template_return_state(
            folder=return_folder,
            search=return_search,
            scroll=return_scroll,
            focus=return_focus,
        ),
    }
    query = urlencode(query_values)
    return RedirectResponse(url=f"/email-templates?{query}", status_code=303)


@router.post("/email-templates/{template_id}/clone")
def clone_email_template(
    template_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        template = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute("emails.templates.clone", {"template_id": template_id, "name": name})
    except ValueError as exc:
        db.rollback()
        query = urlencode({"state": "error", "message": str(exc)})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="clone-hub-email-template",
        result="ok",
        detail=f"Cloned local email template {template_id} as {template.outputs['template_id']}; template content is not retained in the audit log.",
    )
    db.commit()
    query = urlencode({"template": template.outputs['template_id'], "state": "success", "message": "Vorlage wurde als Kopie angelegt."})
    return RedirectResponse(url=f"/email-templates?{query}", status_code=303)


@router.post("/email-templates/{template_id}/delete")
def delete_email_template(
    template_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    confirmation: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    if confirmation.strip().casefold() not in {"löschen", "loeschen"}:
        query = urlencode({"state": "error", "message": "Zum Löschen muss LÖSCHEN bestätigt werden."})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)
    try:
        HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute("emails.templates.delete", {"template_ids": json.dumps([template_id])})
    except ValueError as exc:
        db.rollback()
        query = urlencode({"state": "error", "message": str(exc)})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-email-template",
        result="ok",
        detail=f"Deleted local email template {template_id}; template content is not retained in the audit log.",
    )
    db.commit()
    query = urlencode({"state": "success", "message": "Vorlage wurde im Hub gelöscht."})
    return RedirectResponse(url=f"/email-templates?{query}", status_code=303)


@router.get("/emails/selected", response_class=HTMLResponse)
def mailbox_selected_pane(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    folder: str = "inbox",
    unread: bool = False,
    selected: str = "",
):
    _require_hub_admin(request)
    if folder not in MAILBOX_FOLDERS:
        raise HTTPException(status_code=422, detail="Unknown mailbox folder.")
    mailbox_service = _web_mailbox(request, db)
    selected_message = mailbox_service.get_selected_message(
        folder=folder,
        unread_only=unread,
        selected_key=selected,
    )
    return templates.TemplateResponse(
        request,
        "emails_reading_pane.html",
        {
            "selected": selected_message,
            "linked_case": _mailbox_linked_case(db, selected_message),
            "folder": folder,
            "unread": unread,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/emails/cases/compose", response_class=HTMLResponse)
def mailbox_case_compose(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    source_email_key: str = "",
):
    """Render the native case form for one selected mailbox email."""
    _require_hub_admin(request)
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        source_email, _detail = mailbox_case_context(_email_gateway(request, db), source_email_key)
    except (HubCaseError, HubOperationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "emails_case_compose.html",
        _case_create_context(
            request,
            db,
            selected_customer_id=source_email.customer_id,
            submitted_values={"case_field__case_origin": "E-Mail"},
            source_email=source_email,
        ),
    )


@router.post("/emails/cases", response_class=JSONResponse)
async def create_mailbox_case(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Create and immediately link a Hub case from the selected mailbox email."""
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): str(value)
        for key, value in form.items()
        if isinstance(value, str) and str(key).startswith("case_field__")
    }
    raw_customer_id = str(form.get("customer_id") or "").strip()
    source_email_key = str(form.get("source_email_key") or "").strip()
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        source_email = _case_email_source(db, user, source_email_key)
        case = _execute_case_operation(db, user.username, "create",
            customer_id=raw_customer_id, source_email_key=source_email_key, **submitted_values)
        linked_case = service.linked_case_for_source_email(source_email_key=source_email.key)
        if linked_case is None:
            raise HubCaseError("Die E-Mail konnte nicht mit dem neuen Fall verknüpft werden.")
    except (ValueError, HubCaseError) as exc:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-hub-case-from-mailbox-email",
        result="ok",
        detail=f"Created Hub Case {case.record_id} and linked one selected mailbox email.",
    )
    db.commit()
    return _mailbox_case_payload(linked_case)


@router.get("/emails/cases/{case_id}/compose", response_class=HTMLResponse)
def mailbox_case_edit_compose(
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    source_email_key: str = "",
):
    """Render the linked Hub case as an editor inside the mailbox drawer."""
    _require_hub_admin(request)
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        source_email, detail = mailbox_case_context(_email_gateway(request, db), source_email_key, case_id)
    except (HubCaseError, HubOperationError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    context = _case_create_context(
        request,
        db,
        selected_customer_id=detail.case.customer_id,
        source_email=source_email,
    )
    context.update({"fields": detail.fields, "case_detail": detail})
    return templates.TemplateResponse(request, "emails_case_compose.html", context)


@router.post("/emails/cases/{case_id}", response_class=JSONResponse)
async def update_mailbox_case(
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Save a case from its linked mailbox message without leaving the mailbox."""
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): str(value)
        for key, value in form.items()
        if isinstance(value, str) and str(key).startswith("case_field__")
    }
    raw_customer_id = str(form.get("customer_id") or "").strip()
    source_email_key = str(form.get("source_email_key") or "").strip()
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        if not source_email_key:
            raise HubCaseError("Bitte eine E-Mail auswählen.")
        case = _execute_case_operation(db, user.username, "update",
            case_id=case_id, customer_id=raw_customer_id, source_email_key=source_email_key, **submitted_values)
        linked_case = service.linked_case_for_source_email(source_email_key=source_email_key)
        if linked_case is None:
            raise HubCaseError("Die E-Mail-Verknüpfung wurde nicht gefunden.")
    except (ValueError, HubCaseError) as exc:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-case-from-mailbox-email",
        result="ok",
        detail=f"Updated Hub Case {case.record_id} from one linked mailbox email.",
    )
    db.commit()
    return _mailbox_case_payload(linked_case)


@router.post("/emails/cases/{case_id}/delete", response_class=JSONResponse)
async def delete_mailbox_case(
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Delete the case currently linked to one mailbox message."""
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    source_email_key = str(form.get("source_email_key") or "").strip()
    try:
        if not source_email_key:
            raise HubCaseError("Bitte eine E-Mail auswählen.")
        case = _execute_case_operation(db, user.username, "delete", case_id=case_id, source_email_key=source_email_key)
    except ValueError as exc:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-case-from-mailbox-email",
        result="ok",
        detail=f"Deleted Hub Case {case.record_id} from one linked mailbox email.",
    )
    db.commit()
    return {"case_id": case_id, "source_email_key": source_email_key}


@router.get("/customers/{customer_id}/cases/{case_id}/compose", response_class=HTMLResponse)
def customer_case_edit_compose(
    customer_id: int,
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Render a customer-linked case editor in the customer detail drawer."""
    user = _require_hub_admin(request)
    try:
        detail = HubCrmReadService(db=db, cipher=get_secret_cipher(), actor=user.username).case_detail(case_id, customer_id=customer_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail="Der Fall wurde bei diesem Kunden nicht gefunden.") from exc
    context = _case_create_context(request, db, selected_customer_id=customer_id)
    context.update(
        {
            "fields": detail.fields,
            "case_detail": detail,
            "case_compose_mode": "customer",
            "case_compose_action": f"/customers/{customer_id}/cases/{case_id}",
        }
    )
    return templates.TemplateResponse(request, "emails_case_compose.html", context)


@router.post("/customers/{customer_id}/cases/{case_id}", response_class=JSONResponse)
async def update_customer_case(
    customer_id: int,
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Save a case from its customer's detail drawer."""
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): str(value)
        for key, value in form.items()
        if isinstance(value, str) and str(key).startswith("case_field__")
    }
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        existing_detail = service.get_detail(case_id=case_id)
        if existing_detail is None or existing_detail.case.customer_id != customer_id:
            raise HubCaseError("Der Fall wurde bei diesem Kunden nicht gefunden.")
        case = _execute_case_operation(db, user.username, "update", case_id=case_id, customer_id=customer_id, **submitted_values)
        was_completed_now = case.outputs["completed_now"] == "true"
    except ValueError as exc:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-case-from-customer",
        result="ok",
        detail=f"Updated Hub Case {case.record_id} from its customer detail page.",
    )
    db.commit()
    return {
        "case_id": case.record_id,
        "case_number": case.outputs["case_number"],
        "completion_email": was_completed_now,
    }


@router.post("/customers/{customer_id}/cases/{case_id}/delete", response_class=JSONResponse)
async def delete_customer_case(
    customer_id: int,
    case_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Delete a case from its customer detail drawer without touching Zoho."""
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        detail = service.get_detail(case_id=case_id)
        if detail is None or detail.case.customer_id != customer_id:
            raise HubCaseError("Der Fall wurde bei diesem Kunden nicht gefunden.")
        case = _execute_case_operation(db, user.username, "delete", case_id=case_id)
    except ValueError as exc:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-case-from-customer",
        result="ok",
        detail=f"Deleted Hub Case {case.record_id} from its customer detail page.",
    )
    db.commit()
    return {"case_id": case_id}


@router.get("/emails/folder", response_class=HTMLResponse)
def mailbox_folder_panel(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    folder: str = "inbox",
    unread: bool = False,
    selected: str = "",
):
    _require_hub_admin(request)
    if folder not in MAILBOX_FOLDERS:
        raise HTTPException(status_code=422, detail="Unknown mailbox folder.")
    mailbox_service = _web_mailbox(request, db)
    return templates.TemplateResponse(
        request,
        "emails_folder_panel.html",
        {
            "mailbox_account_id": mailbox_service.account_id,
            "mailbox": mailbox_service.get_folder_view(
                folder=folder,
                unread_only=unread,
                selected_key=selected,
            ),
            "folder": folder,
            "unread": unread,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/emails/list", response_class=HTMLResponse)
def mailbox_folder_list(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    folder: str = "inbox",
    unread: bool = False,
):
    """Return header-only mailbox data for client-side folder preloading."""
    _require_hub_admin(request)
    if folder not in MAILBOX_FOLDERS:
        raise HTTPException(status_code=422, detail="Unknown mailbox folder.")
    mailbox_service = _web_mailbox(request, db)
    mailbox = mailbox_service.get_folder_view(folder=folder, unread_only=unread, load_selected=False)
    return templates.TemplateResponse(
        request,
        "emails_message_list.html",
        {
            "mailbox_account_id": mailbox_service.account_id,
            "messages": mailbox.messages,
            "selected": None,
            "folder": folder,
            "unread": unread,
        },
    )


@router.get("/emails/status", response_class=JSONResponse)
def mailbox_status(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Provide mailbox counters for a live UI refresh without returning message data."""
    _require_hub_admin(request)
    _web_mailbox(request, db)
    return mailbox_status_data(_email_gateway(request, db), _mailbox_account_id(request))


@router.post("/emails/actions", response_class=JSONResponse)
def apply_mailbox_batch_action(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    action: Annotated[str, Form()] = "",
    keys: Annotated[list[str] | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    """Apply one contextual mailbox action to the current multi-selection."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    mailbox = _web_mailbox(request, db)
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            f"emails.mailbox.{action}", {"email_keys": json.dumps(keys or [])},
        )
        changed_count = int(result.outputs["changed_count"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action=f"mailbox-{action}",
        result="ok",
        detail=f"Applied mailbox action to {changed_count} stored email record(s).",
    )
    db.commit()
    return {
        "changed_count": changed_count,
        **mailbox_status_data(_email_gateway(request, db), mailbox.account_id),
    }


@router.get("/emails/compose/options", response_class=JSONResponse)
def mailbox_compose_options(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    _web_mailbox(request, db)
    return email_compose_options(_email_gateway(request, db), _mailbox_account_id(request))


@router.post("/emails/ai/rewrite", response_class=JSONResponse)
def rewrite_selected_mailbox_email_text(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    instruction: Annotated[str, Form()] = "",
    selected_html: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    """Prepare one reviewable rewrite for a text selection in an unsent email."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        proposal = EmailAiRewriteService(db=db, cipher=get_secret_cipher()).rewrite(
            actor=user.username,
            instruction=instruction,
            selected_html=selected_html,
        )
    except EmailAiRewriteError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-email-editor",
        action="prepare-email-ai-rewrite",
        result="success",
        detail="Prepared one reviewable AI rewrite for a selected email fragment; content is omitted from the audit log.",
    )
    db.commit()
    return {"replacement_html": proposal.replacement_html}


@router.post("/emails/compose/images", response_class=JSONResponse)
async def upload_mailbox_compose_image(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    image: Annotated[UploadFile | None, File()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    """Store one composer image locally; it is embedded as a CID part only when sent."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    if image is None or not image.filename:
        raise HTTPException(status_code=400, detail="Wähle ein Bild zum Einfügen aus.")
    try:
        stored = EmailComposeImageService(db=db, cipher=get_secret_cipher()).store_upload(
            filename=image.filename,
            content=await image.read(5 * 1024 * 1024 + 1),
            submitted_content_type=image.content_type,
            user_id=user.id,
        )
    except EmailComposeImageError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await image.close()

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="upload-email-compose-image",
        result="ok",
        detail="Stored one local email-compose image; image content is not retained in the audit log.",
    )
    db.commit()
    return {"success": True, "url": f"/emails/compose/images/{stored.token}"}


@router.get("/emails/compose/images/{token}")
def load_mailbox_compose_image(
    token: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Render a local image in the authenticated editor or the Hub's sent-mail preview."""
    _require_hub_admin(request)
    try:
        image, content = EmailComposeImageService(db=db, cipher=get_secret_cipher()).load_image(token=token)
    except EmailComposeImageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=content, media_type=image.content_type, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/emails/compose/recipients", response_class=JSONResponse)
def mailbox_compose_recipients(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
):
    _require_hub_admin(request)
    return {"recipients": email_recipients(_email_gateway(request, db), query=q)}


@router.get("/emails/compose/templates/{template_id}", response_class=JSONResponse)
def mailbox_compose_template_preview(
    template_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    customer_id: str = "", lead_id: str = "", dunning_id: str = "",
    recipient_key: str = "", recipient_email: str = "",
    context_module: str = "", context_record_id: str = "", context_path: str = "",
):
    """Return a local template even before the recipient context is known."""
    user = _require_hub_admin(request)
    try:
        template = render_template(HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username), {
            "template_id": template_id, "customer_id": customer_id, "lead_id": lead_id, "dunning_id": dunning_id,
            "recipient_key": recipient_key, "recipient_email": recipient_email,
            "context_module": context_module, "context_record_id": context_record_id, "context_path": context_path,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": template.id,
        "name": template.name,
        "subject": template.subject,
        "content": template.content,
        "unresolved_placeholders": template.unresolved_placeholders,
        "template_context": template.template_context,
    }


@router.get("/emails/compose/customers/{customer_id}/recipients", response_class=JSONResponse)
def mailbox_compose_customer_contact_recipients(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Return each stored email address of contacts linked to this customer."""
    _require_hub_admin(request)
    try:
        return {"recipients": email_recipients(_email_gateway(request, db), customer_id=customer_id)}
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/emails/linked/{customer_id}/{email_id}/compose-context", response_class=JSONResponse)
def mailbox_linked_email_compose_context(
    customer_id: int,
    email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    action: str = "",
):
    """Prepare an editable composer context for an opened mailbox message."""
    user = _require_hub_admin(request)
    try:
        context = compose_context(HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username), f"linked-{customer_id}-{email_id}", action)
        db.commit()
        return context
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ZohoCrmError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/emails/drafts/{draft_id}/compose-context", response_class=JSONResponse)
def mailbox_draft_compose_context(
    draft_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    try:
        return HubMailboxService(
            actor=_require_hub_admin(request).username,
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).get_draft_compose_context(draft_id=draft_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/emails/scheduled/{scheduled_email_id}/compose-context", response_class=JSONResponse)
def mailbox_scheduled_email_compose_context(
    scheduled_email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    try:
        return scheduled_context(_email_gateway(request, db), scheduled_email_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/emails/unassigned/{email_id}/compose-context", response_class=JSONResponse)
def mailbox_unassigned_email_compose_context(
    email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    action: str = "",
):
    """Prepare a direct Mittwald reply or forward for an inbound mailbox email."""
    user = _require_hub_admin(request)
    try:
        return compose_context(HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username), f"unassigned-{email_id}", action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/emails/drafts", response_class=JSONResponse)
def save_mailbox_draft(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    draft_id: Annotated[str, Form()] = "",
    sender_email: Annotated[str, Form()] = "",
    recipient_email: Annotated[str, Form()] = "",
    recipient_key: Annotated[str, Form()] = "",
    recipient_customer_id: Annotated[str, Form()] = "",
    lead_id: Annotated[str, Form()] = "",
    dunning_id: Annotated[str, Form()] = "",
    recipient_name: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    cc_emails: Annotated[str, Form()] = "",
    template_id: Annotated[str, Form()] = "",
    context_module: Annotated[str, Form()] = "",
    context_record_id: Annotated[str, Form()] = "",
    reply_to_email_id: Annotated[str, Form()] = "",
    forward_from_email_id: Annotated[str, Form()] = "",
    scheduled_at: Annotated[str, Form()] = "",
    retained_attachment_ids: Annotated[str, Form()] = "",
    attachments: Annotated[list[UploadFile] | None, File()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    mailbox = _web_mailbox(request, db)
    try:
        uploaded = []
        total_bytes = 0
        for attachment in attachments or []:
            if not attachment.filename:
                continue
            if len(uploaded) >= 20:
                raise ValueError("Es können höchstens 20 Anhänge pro E-Mail gespeichert werden.")
            file_content = attachment.file.read(50 * 1024 * 1024 - total_bytes + 1)
            total_bytes += len(file_content)
            if total_bytes > 50 * 1024 * 1024:
                raise ValueError("Die Anhänge sind zusammen größer als 50 MB.")
            uploaded.append(HubArtifact(
                filename=attachment.filename, content=file_content,
                content_type=attachment.content_type or "application/octet-stream",
            ))
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username, input_files=tuple(uploaded)).execute(
            "emails.drafts.save", {
                "draft_id": draft_id, "sender_email": sender_email, "recipient_email": recipient_email,
                "recipient_key": recipient_key, "recipient_customer_id": recipient_customer_id,
                "lead_id": lead_id,
                "dunning_id": dunning_id, "recipient_name": recipient_name, "subject": subject,
                "content": content, "cc_emails": cc_emails, "template_id": template_id,
                "context_module": context_module, "context_record_id": context_record_id,
                "reply_to_email_id": reply_to_email_id, "forward_from_email_id": forward_from_email_id,
                "scheduled_at": scheduled_at,
                "retained_attachment_ids": retained_attachment_ids,
            },
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        for attachment in attachments or []:
            attachment.file.close()
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="save-mailbox-email-draft",
        result="ok",
        detail=f"Saved mailbox email draft {result.record_id}; email content is not retained in the audit log.",
    )
    db.commit()
    return {
        "draft_id": result.record_id,
        "attachments": HubMailboxService(db=db, cipher=get_secret_cipher(), actor=user.username,
            public_base_url=get_settings().public_base_url).get_draft_compose_context(draft_id=result.record_id)["attachments"],
        "uploaded_attachment_ids": [mailbox.draft_attachment_id(result.record_id, item) for item in uploaded],
        **mailbox_status_data(_email_gateway(request, db), mailbox.account_id),
    }


@router.post("/emails/send")
async def send_direct_mailbox_email(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    sender_email: Annotated[str, Form()] = "",
    recipient_email: Annotated[str, Form()] = "",
    lead_id: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    cc_emails: Annotated[str, Form()] = "",
    draft_id: Annotated[str, Form()] = "",
    retained_attachment_ids: Annotated[str, Form()] = "",
    reply_to_email_id: Annotated[str, Form()] = "",
    forward_from_email_id: Annotated[str, Form()] = "",
    scheduled_at: Annotated[str, Form()] = "",
    scheduled_email_id: Annotated[str, Form()] = "",
    mailbox_origin: Annotated[str, Form()] = "",
    attachments: Annotated[list[UploadFile] | None, File()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    """Deliver an email without a customer selection through Mittwald SMTP."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    mailbox = HubMailboxService(
        actor=_require_hub_admin(request).username,
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )
    try:
        parsed_lead_id = _optional_form_id(lead_id)
        if parsed_lead_id is not None:
            from app.services.hub_deletion import lock_parent
            if not HubAccessControlService(db=db).can_access_record(user=user, module_key="leads", record_id=parsed_lead_id):
                raise ValueError("Der verknuepfte Lead ist nicht verfuegbar.")
            lock_parent(db, kind="leads", record_id=parsed_lead_id)
        reply_to_id = int(reply_to_email_id) if reply_to_email_id.strip() else None
        forward_from_id = int(forward_from_email_id) if forward_from_email_id.strip() else None
        parsed_scheduled_email_id = int(scheduled_email_id) if scheduled_email_id.strip() else None
        if parsed_scheduled_email_id is not None and not scheduled_at.strip():
            raise ValueError("Wähle für die geplante E-Mail einen Versandzeitpunkt aus.")
        uploaded_attachments: list[CustomerCommunicationAttachmentUpload] = []
        for attachment in attachments or []:
            if not attachment.filename:
                continue
            uploaded_attachments.append(
                CustomerCommunicationAttachmentUpload(
                    filename=attachment.filename,
                    content=await attachment.read(),
                    content_type=attachment.content_type or "application/octet-stream",
                )
            )
        if draft_id.strip().isdigit():
            uploaded_attachments.extend(mailbox.prepare_draft_delivery_attachments(
                draft_id=int(draft_id), retained_attachment_ids=mailbox.parse_retained_attachment_ids(retained_attachment_ids),
            ))
        if scheduled_at.strip():
            scheduled = ScheduledEmailService(
                db=db,
                cipher=get_secret_cipher(),
                public_base_url=get_settings().public_base_url,
            ).schedule(
                actor=user.username,
                lead_id=parsed_lead_id,
                scheduled_at=ScheduledEmailService.parse_berlin_datetime(scheduled_at),
                sender_email=sender_email,
                recipient_email=recipient_email,
                recipient_name="",
                subject=subject,
                content=content,
                cc_emails=cc_emails,
                reply_to_email_id=reply_to_id,
                forward_from_email_id=forward_from_id,
                attachments=tuple(uploaded_attachments),
                scheduled_email_id=parsed_scheduled_email_id,
            )
            sent = None
        else:
            scheduled = None
            sent = mailbox.send_direct_email(
                sender_email=sender_email,
                recipient_email=recipient_email,
                subject=subject,
                content=content,
                cc_emails=cc_emails,
                attachments=tuple(uploaded_attachments),
                reply_to_email_id=reply_to_id,
                forward_from_email_id=forward_from_id,
            )
            if parsed_lead_id is not None:
                sent_payload = mailbox._payload(sent.encrypted_payload_json)
                sent_payload["recipient_lead_id"] = parsed_lead_id
                sent.encrypted_payload_json = get_secret_cipher().encrypt(json.dumps(sent_payload, ensure_ascii=False))
        if draft_id.strip().isdigit():
            mailbox.discard_draft(draft_id=int(draft_id))
    except ValueError as exc:
        db.rollback()
        return _email_compose_response(request, RedirectResponse(
            url=f"/emails?{urlencode({'folder': 'planned' if scheduled_email_id.strip() else 'sent', 'email_state': 'error', 'email_message': str(exc)})}",
            status_code=303,
        ), error=str(exc))
    finally:
        for attachment in attachments or []:
            await attachment.close()

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="schedule-direct-email" if scheduled is not None else "send-direct-mittwald-email",
        result="ok",
        detail=(
            f"Scheduled unlinked mailbox email {scheduled.id}; recipients and content are not retained in the audit log."
            if scheduled is not None
            else "Sent an unlinked mailbox email through Mittwald; recipients and content are not retained in the audit log."
        ),
    )
    db.commit()
    if scheduled is not None:
        ScheduledEmailWorker.notify_schedule_changed()
        return _email_compose_response(request, RedirectResponse(
            url=f"/emails?{urlencode({'folder': 'planned', 'selected': f'scheduled-{scheduled.id}', 'email_state': 'success', 'email_message': 'E-Mail wurde für den geplanten Versand gespeichert.'})}",
            status_code=303,
        ))
    assert sent is not None
    return _email_compose_response(request, RedirectResponse(
        url=f"/emails?{urlencode({'folder': 'sent', 'selected': f'unassigned-{sent.id}', 'email_state': 'success', 'email_message': 'E-Mail wurde über Mittwald versendet.'})}",
        status_code=303,
    ))


@router.post("/emails/scheduled/{scheduled_email_id}/send-now")
def send_scheduled_mailbox_email_now(
    scheduled_email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    service = ScheduledEmailService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )
    try:
        HubMailboxAccess(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).require(f"scheduled-{scheduled_email_id}")
        result = service.send_now(scheduled_email_id=scheduled_email_id)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/emails?{urlencode({'folder': 'planned', 'email_state': 'error', 'email_message': str(exc)})}",
            status_code=303,
        )
    scheduled = db.get(HubScheduledEmail, scheduled_email_id)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="send-scheduled-email-now",
        result="ok" if result.sent else "failed",
        detail=f"Requested immediate delivery for scheduled email {scheduled_email_id}; recipients and content are not retained in the audit log.",
    )
    db.commit()
    if result.sent and scheduled is not None:
        if scheduled.customer_id is not None and scheduled.customer_email_id is not None:
            selected = f"linked-{scheduled.customer_id}-{scheduled.customer_email_id}"
        else:
            selected = f"unassigned-{scheduled.mailbox_email_id}" if scheduled.mailbox_email_id is not None else ""
        return RedirectResponse(
            url=f"/emails?{urlencode({'folder': 'sent', 'selected': selected, 'email_state': 'success', 'email_message': 'E-Mail wurde sofort versendet.'})}",
            status_code=303,
        )
    message = scheduled.last_error if scheduled is not None and scheduled.last_error else "Der Versand wird erneut versucht."
    return RedirectResponse(
        url=f"/emails?{urlencode({'folder': 'planned', 'selected': f'scheduled-{scheduled_email_id}', 'email_state': 'error', 'email_message': message})}",
        status_code=303,
    )


@router.post("/emails/scheduled/{scheduled_email_id}/cancel")
def cancel_scheduled_mailbox_email(
    scheduled_email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        _email_gateway(request, db).execute("emails.scheduled.cancel", {"scheduled_email_id": str(scheduled_email_id)})
    except ValueError as exc:
        return RedirectResponse(
            url=f"/emails?{urlencode({'folder': 'planned', 'email_state': 'error', 'email_message': str(exc)})}",
            status_code=303,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="cancel-scheduled-email",
        result="ok",
        detail=f"Cancelled scheduled email {scheduled_email_id}; recipients and content are not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(
        url=f"/emails?{urlencode({'folder': 'planned', 'email_state': 'success', 'email_message': 'Geplanter Versand wurde abgebrochen.'})}",
        status_code=303,
    )


@router.get("/emails/scheduled/{scheduled_email_id}/attachments/{attachment_id}")
def download_scheduled_mailbox_attachment(
    scheduled_email_id: int,
    attachment_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _require_hub_admin(request)
    try:
        download = download_shared_email_attachment(_email_gateway(request, db), f"scheduled-{scheduled_email_id}", str(attachment_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="download-scheduled-email-attachment",
        result="ok",
        detail=f"Downloaded an attachment for scheduled email {scheduled_email_id}.",
    )
    db.commit()
    return Response(
        content=download.content,
        media_type=download.content_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download.filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/emails/unassigned/{email_id}/read")
def mark_unassigned_mailbox_email_read(
    email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    _require_hub_admin(request)
    try:
        _email_gateway(request, db).execute("emails.mailbox.mark_read", {"email_keys": json.dumps([f"unassigned-{email_id}"])})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return Response(status_code=204)


@router.get("/emails/unassigned/{email_id}/attachments/{attachment_id}")
def download_unassigned_mailbox_attachment(
    email_id: int,
    attachment_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _require_hub_admin(request)
    try:
        download = download_shared_email_attachment(_email_gateway(request, db), f"unassigned-{email_id}", attachment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="download-mittwald-unassigned-email-attachment",
        result="ok",
        detail=f"Downloaded a locally secured attachment for unassigned mailbox email {email_id}.",
    )
    db.commit()
    return Response(
        content=download.content,
        media_type=download.content_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download.filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/emails/linked/{customer_id}/{email_id}/load")
def load_linked_mailbox_email_content(
    customer_id: int,
    email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    folder: Annotated[str, Form()] = "inbox",
    unread: Annotated[bool, Form()] = False,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    _require_hub_admin(request)
    if folder not in MAILBOX_FOLDERS:
        raise HTTPException(status_code=422, detail="Unknown mailbox folder.")
    try:
        HubMailboxAccess(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).require(f"linked-{customer_id}-{email_id}")
        _customer_communication_service(db).load_email_content(customer_id=customer_id, email_id=email_id)
    except (ValueError, ZohoCrmError):
        db.rollback()
    else:
        db.commit()
    return RedirectResponse(
        url=_mailbox_url(folder=folder, unread=unread, selected=f"linked-{customer_id}-{email_id}"),
        status_code=303,
    )


@router.get("/customers/{customer_id}/website-profile/preview", response_class=JSONResponse)
def customer_website_profile_preview(customer_id: int, request: Request, db: Annotated[Session, Depends(get_db)], site_id: str = ""):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        data = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).query(
            "customers.website_profile.preview", {"customer_id": str(customer_id), "site_id": site_id})
    except HubOperationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(data, headers={"Cache-Control": "private, no-store"})


@router.post("/customers/{customer_id}/website-profile/send", response_class=JSONResponse)
def customer_website_profile_send(customer_id: int, request: Request, db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[int, Form()], preview_token: Annotated[str, Form()],
    field_ids: Annotated[list[str], Form()] = [], confirmed: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = ""):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if confirmed != "yes":
        raise HTTPException(status_code=422, detail="Bitte die Uebertragung bestaetigen.")
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            "wordpress.company_profile.send", {"customer_id": str(customer_id), "site_id": str(site_id),
                "preview_token": preview_token, "field_ids": json.dumps(field_ids)})
        db.commit()
    except HubOperationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"job_id": result.record_id, "href": result.href, "status": "queued"})


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail_page(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    communication: str = "",
    message: str = "",
    fields: str = "",
    fields_message: str = "",
    layout: str = "",
    layout_message: str = "",
    activity: str = "",
    activity_message: str = "",
    completion_email: bool = False,
):
    cipher = get_secret_cipher()
    current_user = getattr(request.state, "hub_user", None)
    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    access = HubAccessControlService(db=db)
    can_view_emails = access.can(current_user, "emails", "view")
    can_view_contacts = access.can(current_user, "contacts", "view")
    can_view_cases = access.can(current_user, "cases", "view")
    can_view_finance = access.can(current_user, "finance", "view")
    can_view_activities = access.can(current_user, "activities", "view")
    can_view_websites = access.can(current_user, "websites", "view")
    can_manage_customer_fields = access.can_access_record(
        user=current_user,
        module_key="customers",
        record_id=customer_id,
        action="edit",
    )
    try:
        detail = read_customer_detail(HubOperationService(db=db, cipher=cipher, actor=current_user.username), {"customer_id": str(customer_id)})
    except HubOperationError:
        raise HTTPException(status_code=404, detail="Customer not found.")
    communication_service = CustomerCommunicationService(
        db=db,
        cipher=cipher,
        public_base_url=get_settings().public_base_url,
    )
    can_manage_communications = can_view_emails and access.can(current_user, "emails", "create")
    communication_view = communication_service.get_view(customer_id=customer_id, actor=current_user.username) if can_view_emails else None
    linked_cases_by_email_id = (
        HubCaseService(db=db, cipher=cipher).linked_cases_for_customer_emails(customer_id=customer_id)
        if can_view_emails and can_view_cases else {}
    )
    communication_state = communication if communication in {"success", "warning", "error"} else ""
    activity_state = activity if activity in {"success", "error"} else ""
    berlin_now = datetime.now(ZoneInfo("Europe/Berlin"))
    call_start = suggested_call_start(berlin_now).replace(tzinfo=None)
    activity_service = CustomerActivityService(db=db)
    finance_service = HubFinanceService(db=db, cipher=cipher)
    finance_document_service = HubFinanceDocumentService(db=db, cipher=cipher)
    return templates.TemplateResponse(
        request,
        "customer_detail.html",
        {
            "detail": detail,
            "communication": communication_view,
            "linked_cases_by_email_id": linked_cases_by_email_id,
            "communication_state": communication_state,
            "communication_message": message[:500] if communication_state else "",
            "can_manage_communications": can_manage_communications,
            "can_manage_customer_fields": can_manage_customer_fields,
            "can_send_website_profile": can_manage_customer_fields and access.can(current_user, "websites", "edit"),
            "can_view_emails": can_view_emails,
            "can_view_contacts": can_view_contacts,
            "can_create_contacts": can_view_contacts and access.can(current_user, "contacts", "create"),
            "can_view_cases": can_view_cases,
            "can_create_cases": can_view_cases and access.can(current_user, "cases", "create"),
            "can_view_finance": can_view_finance,
            "can_create_finance": can_view_finance and access.can(current_user, "finance", "create"),
            "can_view_activities": can_view_activities,
            "can_create_activities": can_view_activities and access.can(current_user, "activities", "create"),
            "can_edit_activities": can_view_activities and access.can(current_user, "activities", "edit"),
            "can_delete_activities": can_view_activities and access.can(current_user, "activities", "delete"),
            "can_view_websites": can_view_websites,
            "active_customer_communication_tab": next((
                key for key, allowed in (
                    ("emails", can_view_emails),
                    ("contacts", can_view_contacts),
                    ("cases", can_view_cases),
                    ("orders", can_view_finance),
                ) if allowed
            ), ""),
            "fields_state": fields if fields in {"success", "error"} else "",
            "fields_message": fields_message[:500] if fields in {"success", "error"} else "",
            "layout_state": layout if layout in {"success", "error"} else "",
            "layout_message": layout_message[:500] if layout in {"success", "error"} else "",
            "activity_calls": ActivityResponsibility(db, current_user).filter_views("call", activity_service.list_calls(customer_id=customer_id)) if can_view_activities else (),
            "activity_tasks": ActivityResponsibility(db, current_user).filter_views("task", activity_service.list_tasks(customer_id=customer_id)) if can_view_activities else (),
            "activity_meetings": ActivityResponsibility(db, current_user).filter_views("meeting", activity_service.list_meetings(customer_id=customer_id)) if can_view_activities else (),
            **ActivityResponsibility(db, current_user).ui_context(),
            "activity_state": activity_state,
            "activity_message": activity_message[:500] if activity_state else "",
            "customer_finance_offers": finance_service.list_customer_offers(customer_id=customer_id) if can_view_finance else (),
            "customer_finance_orders": finance_document_service.list_customer_documents(
                module=ORDER_MODULE, customer_id=customer_id
            ) if can_view_finance else (),
            "customer_finance_invoices": finance_document_service.list_customer_documents(
                module=INVOICE_MODULE, customer_id=customer_id
            ) if can_view_finance else (),
            "customer_finance_recurring_invoices": finance_document_service.list_customer_documents(
                module=RECURRING_INVOICE_MODULE, customer_id=customer_id
            ) if can_view_finance else (),
            "customer_finance_dunnings": finance_document_service.list_customer_documents(
                module=DUNNING_MODULE, customer_id=customer_id
            ) if can_view_finance else (),
            "call_status_options": CALL_STATUS_OPTIONS,
            "call_direction_options": CALL_DIRECTION_OPTIONS,
            "call_duration_options": CALL_DURATION_OPTIONS,
            "call_time_options": CALL_TIME_OPTIONS,
            "call_reminder_channel_options": CALL_REMINDER_CHANNEL_OPTIONS,
            "call_reminder_options": CALL_REMINDER_OPTIONS,
            **activity_form_defaults(now=berlin_now, start=call_start),
            "completion_email_template_id": CASE_COMPLETION_EMAIL_TEMPLATE_ID if completion_email else "",
            "completion_email_customer_id": customer_id if completion_email else None,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.post("/customers/{customer_id}/fields")
async def update_customer_fields(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    service = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username)
    try:
        detail = read_customer_detail(service, {"customer_id": str(customer_id)}, action="edit")
        submitted = {str(key): value for key, value in form.multi_items() if isinstance(value, str)}
        for field in detail.editable_profile_fields:
            if field.display_type == "Boolesch":
                submitted.setdefault(f"customer_field__{field.key}", "false")
        result = service.execute("customers.update", {
            **record_form_input(submitted, kind="customer"), "customer_id": str(customer_id),
        })
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/customers/{customer_id}?{query}#customer-fields", status_code=303)
    customer = db.get(Customer, result.record_id)
    is_zoho = bool(customer.zoho_id)
    write_audit_log(
        db, site=None, actor=user.username, source="hub-web",
        action="update-zoho-customer-fields" if is_zoho else "update-hub-customer-fields",
        result="ok", detail=f"Updated Customer {customer.id}; customer data is not retained in the audit log.",
    )
    db.commit()
    query = urlencode({"fields": "success", "fields_message": "Kundendaten wurden in Zoho CRM gespeichert." if is_zoho else "Kundendaten wurden im Hub gespeichert."})
    return RedirectResponse(url=f"/customers/{customer_id}?{query}#customer-fields", status_code=303)

@router.post("/customers/{customer_id}/layout")
async def update_customer_field_layout(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    csrf_token = form.get("csrf_token")
    require_csrf(request, csrf_token if isinstance(csrf_token, str) else "")
    user = _require_hub_admin(request)
    detail = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).get_detail(
        customer_id=customer_id,
        include_sensitive=True,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    try:
        _save_module_layout(request, db, CUSTOMER_FIELDS_LAYOUT_KEY, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        query = urlencode({"layout": "error", "layout_message": str(exc)})
        return RedirectResponse(url=f"/customers/{customer_id}?{query}#customer-fields", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-customer-fields-layout",
        result="ok",
        detail="Updated the global customer field layout.",
    )
    db.commit()
    query = urlencode({"layout": "success", "layout_message": "Das globale Kundenfelder-Layout wurde gespeichert."})
    return RedirectResponse(url=f"/customers/{customer_id}?{query}#customer-fields", status_code=303)


@router.post("/customers/{customer_id}/communications/sync")
def sync_customer_communications(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        result = _customer_communication_service(db).sync_customer(customer_id=customer_id)
    except (ValueError, ZohoCrmError) as exc:
        db.rollback()
        return _customer_communication_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="sync-zoho-customer-communications",
        result="ok",
        detail=f"Synchronized Zoho communication headers for customer {customer_id}: {result.notes} new notes, {result.emails} new emails.",
    )
    db.commit()
    return _customer_communication_redirect(
        customer_id,
        "success",
        f"Zoho-Kommunikation aktualisiert: {result.notes} neue Notizen, {result.emails} neue E-Mail-Köpfe.",
    )


@router.post("/customers/{customer_id}/communications/notes")
def create_customer_communication_note(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    title: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        result = _execute_note_operation(db, user.username, "customers", "create",
            customer_id=customer_id,
            title=title,
            content=content,
        )
    except (ValueError, ZohoCrmError) as exc:
        db.rollback()
        return _customer_communication_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="create-zoho-customer-note",
        result="ok" if (result.outputs["sync_status"] != "failed") else "failed",
        detail=f"Created customer note for customer {customer_id}; note content is not retained in the audit log.",
    )
    db.commit()
    return _customer_communication_redirect(customer_id, "success" if (result.outputs["sync_status"] != "failed") else "warning", result.outputs["message"])


@router.post("/customers/{customer_id}/communications/notes/{note_id}")
def update_customer_communication_note(
    customer_id: int,
    note_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    title: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        result = _execute_note_operation(db, user.username, "customers", "update",
            customer_id=customer_id,
            note_id=note_id,
            title=title,
            content=content,
        )
    except (ValueError, ZohoCrmError) as exc:
        db.rollback()
        return _customer_communication_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-zoho-customer-note",
        result="ok" if (result.outputs["sync_status"] != "failed") else "failed",
        detail=f"Updated customer note {note_id} for customer {customer_id}; note content is not retained in the audit log.",
    )
    db.commit()
    return _customer_communication_redirect(customer_id, "success" if (result.outputs["sync_status"] != "failed") else "warning", result.outputs["message"])


@router.post("/customers/{customer_id}/communications/notes/{note_id}/delete")
def delete_customer_communication_note(
    customer_id: int,
    note_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        result = _execute_note_operation(db, user.username, "customers", "delete", customer_id=customer_id, note_id=note_id)
    except (ValueError, ZohoCrmError) as exc:
        db.rollback()
        return _customer_communication_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-zoho-customer-note",
        result="ok" if (result.outputs["sync_status"] != "failed") else "failed",
        detail=f"Deleted customer note {note_id} for customer {customer_id}.",
    )
    db.commit()
    return _customer_communication_redirect(customer_id, "success" if (result.outputs["sync_status"] != "failed") else "warning", result.outputs["message"])


@router.post("/customers/{customer_id}/activities/calls")
def schedule_customer_call(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "planned",
    direction: Annotated[str, Form()] = "outbound",
    start_date: Annotated[str, Form()] = "",
    start_time: Annotated[str, Form()] = "",
    duration_minutes: Annotated[str, Form()] = "30",
    reminder_channels: Annotated[list[str] | None, Form()] = None,
    reminder_minutes_before: Annotated[list[str] | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        call = _execute_activity_operation(db, user.username, "call", "create",
            assignee_user_id=assignee_user_id,
            customer_id=customer_id,
            name=name,
            status=status,
            direction=direction,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels or [],
            reminder_minutes_before=reminder_minutes_before or [],
            description=description,
        )
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="schedule-customer-call",
        result="ok",
        detail=f"Scheduled customer call {call.record_id} for customer {customer_id}; call description is not retained in the audit log.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Anruf wurde geplant.")


@router.post("/customers/{customer_id}/activities/calls/{call_id}")
def update_customer_call(
    customer_id: int,
    call_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "planned",
    direction: Annotated[str, Form()] = "outbound",
    start_date: Annotated[str, Form()] = "",
    start_time: Annotated[str, Form()] = "",
    duration_minutes: Annotated[str, Form()] = "30",
    reminder_channels: Annotated[list[str] | None, Form()] = None,
    reminder_minutes_before: Annotated[list[str] | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        call = _execute_activity_operation(db, user.username, "call", "update",
            assignee_user_id=assignee_user_id,
            customer_id=customer_id,
            activity_id=call_id,
            name=name,
            status=status,
            direction=direction,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels or [],
            reminder_minutes_before=reminder_minutes_before or [],
            description=description,
        )
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-customer-call",
        result="ok",
        detail=f"Updated customer call {call.record_id} for customer {customer_id}; call description is not retained in the audit log.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Anruf wurde gespeichert.")


@router.post("/customers/{customer_id}/activities/calls/{call_id}/delete")
def delete_customer_call(
    customer_id: int,
    call_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        call = _execute_activity_operation(db, user.username, "call", "delete", customer_id=customer_id, activity_id=call_id)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-customer-call",
        result="ok",
        detail=f"Deleted customer call {call.record_id} for customer {customer_id}.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Anruf wurde gelöscht.")


@router.post("/customers/{customer_id}/activities/calls/{call_id}/complete")
def complete_customer_call(
    customer_id: int,
    call_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        call = _execute_activity_operation(db, user.username, "call", "complete", customer_id=customer_id, activity_id=call_id)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="complete-customer-call",
        result="ok",
        detail=f"Completed customer call {call.record_id} for customer {customer_id}.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Anruf wurde abgeschlossen.")


@router.post("/customers/{customer_id}/activities/tasks")
def schedule_customer_task(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "planned",
    due_date: Annotated[str, Form()] = "",
    due_time: Annotated[str, Form()] = "",
    reminder_channel: Annotated[str, Form()] = "email",
    reminder_minutes_before: Annotated[str, Form()] = "0",
    description: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        task = _execute_activity_operation(db, user.username, "task", "create",
            assignee_user_id=assignee_user_id,
            customer_id=customer_id,
            name=name,
            status=status,
            due_date=due_date,
            due_time=due_time,
            reminder_channel=reminder_channel,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="schedule-customer-task",
        result="ok",
        detail=f"Scheduled customer task {task.record_id} for customer {customer_id}; task description is not retained in the audit log.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Aufgabe wurde angelegt.")


@router.post("/customers/{customer_id}/activities/tasks/{task_id}")
def update_customer_task(
    customer_id: int,
    task_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "planned",
    due_date: Annotated[str, Form()] = "",
    due_time: Annotated[str, Form()] = "",
    reminder_channel: Annotated[str, Form()] = "email",
    reminder_minutes_before: Annotated[str, Form()] = "0",
    description: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        task = _execute_activity_operation(db, user.username, "task", "update",
            assignee_user_id=assignee_user_id,
            customer_id=customer_id,
            activity_id=task_id,
            name=name,
            status=status,
            due_date=due_date,
            due_time=due_time,
            reminder_channel=reminder_channel,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-customer-task",
        result="ok",
        detail=f"Updated customer task {task.record_id} for customer {customer_id}; task description is not retained in the audit log.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Aufgabe wurde gespeichert.")


@router.post("/customers/{customer_id}/activities/tasks/{task_id}/delete")
def delete_customer_task(
    customer_id: int,
    task_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        task = _execute_activity_operation(db, user.username, "task", "delete", customer_id=customer_id, activity_id=task_id)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-customer-task",
        result="ok",
        detail=f"Deleted customer task {task.record_id} for customer {customer_id}.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Aufgabe wurde gelöscht.")


@router.post("/customers/{customer_id}/activities/tasks/{task_id}/complete")
def complete_customer_task(
    customer_id: int,
    task_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        task = _execute_activity_operation(db, user.username, "task", "complete", customer_id=customer_id, activity_id=task_id)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="complete-customer-task",
        result="ok",
        detail=f"Completed customer task {task.record_id} for customer {customer_id}.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Aufgabe wurde abgeschlossen.")


@router.post("/customers/{customer_id}/activities/meetings")
def schedule_customer_meeting(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "planned",
    start_date: Annotated[str, Form()] = "",
    start_time: Annotated[str, Form()] = "",
    duration_minutes: Annotated[str, Form()] = "60",
    reminder_channels: Annotated[list[str] | None, Form()] = None,
    reminder_minutes_before: Annotated[list[str] | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        meeting = _execute_activity_operation(db, user.username, "meeting", "create",
            assignee_user_id=assignee_user_id,
            customer_id=customer_id,
            name=name,
            status=status,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels or [],
            reminder_minutes_before=reminder_minutes_before or [],
            description=description,
        )
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="schedule-customer-meeting",
        result="ok",
        detail=f"Scheduled customer meeting {meeting.record_id} for customer {customer_id}; meeting description is not retained in the audit log.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Meeting wurde angelegt.")


@router.post("/customers/{customer_id}/activities/meetings/{meeting_id}")
def update_customer_meeting(
    customer_id: int,
    meeting_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "planned",
    start_date: Annotated[str, Form()] = "",
    start_time: Annotated[str, Form()] = "",
    duration_minutes: Annotated[str, Form()] = "60",
    reminder_channels: Annotated[list[str] | None, Form()] = None,
    reminder_minutes_before: Annotated[list[str] | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        meeting = _execute_activity_operation(db, user.username, "meeting", "update",
            assignee_user_id=assignee_user_id,
            customer_id=customer_id,
            activity_id=meeting_id,
            name=name,
            status=status,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels or [],
            reminder_minutes_before=reminder_minutes_before or [],
            description=description,
        )
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-customer-meeting",
        result="ok",
        detail=f"Updated customer meeting {meeting.record_id} for customer {customer_id}; meeting description is not retained in the audit log.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Meeting wurde gespeichert.")


@router.post("/customers/{customer_id}/activities/meetings/{meeting_id}/delete")
def delete_customer_meeting(
    customer_id: int,
    meeting_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        meeting = _execute_activity_operation(db, user.username, "meeting", "delete", customer_id=customer_id, activity_id=meeting_id)
    except (CustomerActivityError, HubOperationError) as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-customer-meeting",
        result="ok",
        detail=f"Deleted customer meeting {meeting.record_id} for customer {customer_id}.",
    )
    db.commit()
    return _customer_activity_redirect(customer_id, "success", "Meeting wurde gelöscht.")


@router.post("/customers/{customer_id}/communications/emails")
async def send_customer_communication_email(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    sender_email: Annotated[str, Form()] = "",
    recipient_key: Annotated[str, Form()] = "",
    recipient_email: Annotated[str, Form()] = "",
    recipient_name: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    template_id: Annotated[str, Form()] = "",
    reply_to_email_id: Annotated[str, Form()] = "",
    cc_emails: Annotated[str, Form()] = "",
    forward_from_email_id: Annotated[str, Form()] = "",
    draft_id: Annotated[str, Form()] = "",
    retained_attachment_ids: Annotated[str, Form()] = "",
    scheduled_at: Annotated[str, Form()] = "",
    scheduled_email_id: Annotated[str, Form()] = "",
    dunning_id: Annotated[str, Form()] = "",
    mailbox_origin: Annotated[str, Form()] = "",
    attachments: Annotated[list[UploadFile] | None, File()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    parsed_dunning_id: int | None = None
    try:
        reply_to_id = int(reply_to_email_id) if reply_to_email_id.strip() else None
        forward_from_id = int(forward_from_email_id) if forward_from_email_id.strip() else None
        parsed_scheduled_email_id = int(scheduled_email_id) if scheduled_email_id.strip() else None
        parsed_dunning_id = int(dunning_id) if dunning_id.strip() else None
        if parsed_dunning_id is not None:
            dunning = db.get(HubFinanceDunning, parsed_dunning_id)
            if dunning is None or dunning.customer_id != customer_id:
                raise ValueError("Die E-Mail konnte dieser Mahnung nicht zugeordnet werden.")
            if not HubAccessControlService(db=db).can(user, "finance", "view"):
                raise ValueError("Für die Verknüpfung mit einer Mahnung fehlt die Berechtigung.")
        if parsed_scheduled_email_id is not None and not scheduled_at.strip():
            raise ValueError("Wähle für die geplante E-Mail einen Versandzeitpunkt aus.")
        uploaded_attachments: list[CustomerCommunicationAttachmentUpload] = []
        for attachment in attachments or []:
            if not attachment.filename:
                continue
            uploaded_attachments.append(
                CustomerCommunicationAttachmentUpload(
                    filename=attachment.filename,
                    content=await attachment.read(),
                    content_type=attachment.content_type or "application/octet-stream",
                )
            )
        if draft_id.strip().isdigit():
            draft_mailbox = HubMailboxService(
                actor=_require_hub_admin(request).username,
                db=db, cipher=get_secret_cipher(), public_base_url=get_settings().public_base_url,
            )
            draft_context = draft_mailbox.get_draft_compose_context(draft_id=int(draft_id))
            if draft_context["customer_id"] not in (None, customer_id):
                raise ValueError("Der Entwurf gehört zu einem anderen Kunden.")
            uploaded_attachments.extend(draft_mailbox.prepare_draft_delivery_attachments(
                draft_id=int(draft_id), retained_attachment_ids=draft_mailbox.parse_retained_attachment_ids(retained_attachment_ids),
            ))
        if scheduled_at.strip():
            scheduled = ScheduledEmailService(
                db=db,
                cipher=get_secret_cipher(),
                public_base_url=get_settings().public_base_url,
            ).schedule(
                actor=user.username,
                scheduled_at=ScheduledEmailService.parse_berlin_datetime(scheduled_at),
                sender_email=sender_email,
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                subject=subject,
                content=content,
                cc_emails=cc_emails,
                customer_id=customer_id,
                dunning_id=parsed_dunning_id,
                recipient_key=recipient_key,
                template_id=template_id,
                reply_to_email_id=reply_to_id,
                forward_from_email_id=forward_from_id,
                attachments=tuple(uploaded_attachments),
                scheduled_email_id=parsed_scheduled_email_id,
            )
            result = None
        else:
            scheduled = None
            result = _customer_communication_service(db).send_email(
                customer_id=customer_id,
                actor=user.username,
                sender_email=sender_email,
                recipient_key=recipient_key,
                subject=subject,
                content=content,
                template_id=template_id,
                reply_to_email_id=reply_to_id,
                cc_emails=cc_emails,
                forward_from_email_id=forward_from_id,
                attachments=tuple(uploaded_attachments),
                dunning_id=parsed_dunning_id,
            )
        if (scheduled is not None or (result is not None and result.success)) and draft_id.strip().isdigit():
            HubMailboxService(
                actor=_require_hub_admin(request).username,
                db=db,
                cipher=get_secret_cipher(),
                public_base_url=get_settings().public_base_url,
            ).discard_draft(draft_id=int(draft_id))
    except (ValueError, ZohoCrmError) as exc:
        db.rollback()
        if mailbox_origin == "1":
            return _email_compose_response(request, RedirectResponse(
                url=f"/emails?{urlencode({'folder': 'planned' if scheduled_email_id.strip() else 'sent', 'email_state': 'error', 'email_message': str(exc)})}",
                status_code=303,
            ), error=str(exc))
        if parsed_dunning_id is not None:
            return _email_compose_response(request, _dunning_email_redirect(parsed_dunning_id, "error", str(exc)), error=str(exc))
        return _email_compose_response(request, _customer_communication_redirect(customer_id, "error", str(exc)), error=str(exc))
    finally:
        for attachment in attachments or []:
            await attachment.close()

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="schedule-customer-email" if scheduled is not None else "send-customer-email",
        result="ok" if scheduled is not None or (result is not None and result.success) else "failed",
        detail=(
            f"Scheduled customer email {scheduled.id} for customer {customer_id}; recipients and message content are not retained in the audit log."
            if scheduled is not None
            else f"Sent customer email for customer {customer_id}; recipients and message content are not retained in the audit log."
        ),
    )
    db.commit()
    if scheduled is not None:
        ScheduledEmailWorker.notify_schedule_changed()
        if mailbox_origin == "1":
            return _email_compose_response(request, RedirectResponse(
                url=f"/emails?{urlencode({'folder': 'planned', 'selected': f'scheduled-{scheduled.id}', 'email_state': 'success', 'email_message': 'E-Mail wurde für den geplanten Versand gespeichert.'})}",
                status_code=303,
            ))
        if parsed_dunning_id is not None:
            return _email_compose_response(request, _dunning_email_redirect(
                parsed_dunning_id,
                "success",
                "E-Mail wurde für den geplanten Versand gespeichert.",
            ))
        return _email_compose_response(request, _customer_communication_redirect(customer_id, "success", "E-Mail wurde für den geplanten Versand gespeichert."))
    assert result is not None
    if parsed_dunning_id is not None:
        return _email_compose_response(request, _dunning_email_redirect(
            parsed_dunning_id,
            "success" if result.success else "warning",
            result.message,
        ), error=None if result.success else result.message)
    return _email_compose_response(
        request, _customer_communication_redirect(customer_id, "success" if result.success else "warning", result.message),
        error=None if result.success else result.message,
    )


@router.get("/customers/{customer_id}/communications/email-templates/{template_id}", response_class=JSONResponse)
def load_customer_communication_email_template(
    customer_id: int,
    template_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    recipient_key: str = "",
    dunning_id: int | None = None,
    context_module: str = "", context_record_id: str = "", context_path: str = "",
):
    user = _require_hub_admin(request)
    try:
        template = render_template(HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username), {
            "template_id": template_id, "customer_id": str(customer_id), "recipient_key": recipient_key,
            "dunning_id": str(dunning_id or ""),
            "context_module": context_module, "context_record_id": context_record_id, "context_path": context_path,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ZohoCrmError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "id": template.id,
        "name": template.name,
        "subject": template.subject,
        "content": template.content,
        "unresolved_placeholders": template.unresolved_placeholders,
    }


@router.post("/customers/{customer_id}/communications/emails/{email_id}/load")
def load_customer_communication_email(
    customer_id: int,
    email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        result = _customer_communication_service(db).load_email_content(
            customer_id=customer_id,
            email_id=email_id,
        )
    except (ValueError, ZohoCrmError) as exc:
        db.rollback()
        return _customer_communication_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="load-zoho-customer-email-content",
        result="ok",
        detail=f"Loaded encrypted Zoho email content for customer {customer_id}.",
    )
    db.commit()
    return _customer_communication_redirect(customer_id, "success", result.message)


@router.post("/customers/{customer_id}/communications/emails/{email_id}/read")
def mark_customer_communication_email_read(
    customer_id: int,
    email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    if getattr(request.state, "hub_user", None) is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        _email_gateway(request, db).execute("emails.mailbox.mark_read", {"email_keys": json.dumps([f"linked-{customer_id}-{email_id}"])})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return Response(status_code=204)


@router.get("/customers/{customer_id}/communications/emails/{email_id}/attachments/{attachment_id}")
def download_customer_communication_attachment(
    customer_id: int,
    email_id: int,
    attachment_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _require_hub_admin(request)
    try:
        download = download_shared_email_attachment(_email_gateway(request, db), f"linked-{customer_id}-{email_id}", attachment_id, allow_fetch=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ZohoCrmError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="download-zoho-customer-email-attachment",
        result="ok",
        detail=f"Downloaded a Zoho email attachment for customer {customer_id}; attachment data is not retained in the Hub.",
    )
    db.commit()
    return Response(
        content=download.content,
        media_type=download.content_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download.filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/customers/{customer_id}/communications/emails/{email_id}/images/{source_url_hash}")
def display_customer_communication_email_image(
    customer_id: int,
    email_id: int,
    source_url_hash: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        image = _customer_communication_service(db).get_email_preview_image(
            customer_id=customer_id,
            email_id=email_id,
            source_url_hash=source_url_hash,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CustomerCommunicationImageError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail="Das externe Bild konnte nicht sicher geladen werden.") from exc

    db.commit()
    return Response(
        content=image.content,
        media_type=image.content_type,
        headers={
            "Cache-Control": "private, max-age=2592000",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/customers/{customer_id}/contacts/{contact_id}", response_class=HTMLResponse)
def customer_contact_detail_page(
    customer_id: int,
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    fields: str = "",
    fields_message: str = "",
    layout: str = "",
    layout_message: str = "",
):
    try:
        detail = HubCrmReadService(db=db, cipher=get_secret_cipher(), actor=_require_hub_admin(request).username).contact_detail(contact_id, customer_id=customer_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Contact not found for this customer.")
    return templates.TemplateResponse(
        request,
        "customer_contact_detail.html",
        _contact_detail_context(
            request,
            db,
            detail=detail,
            fields=fields,
            fields_message=fields_message,
            layout=layout,
            layout_message=layout_message,
        ),
    )


@router.post("/customers/{customer_id}/contacts/{contact_id}/fields")
async def update_customer_contact_fields(
    customer_id: int,
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): str(value)
        for key, value in form.items()
        if isinstance(value, str) and str(key).startswith("contact_field__")
    }
    try:
        contact = _execute_contact_operation(
            db, user.username, "update", customer_id=customer_id, contact_id=contact_id, **submitted_values,
        )
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/customers/{customer_id}/contacts/{contact_id}?{query}#contact-fields", status_code=303)
    write_audit_log(
        db, site=None, actor=user.username, source="hub-web", action="update-zoho-contact-fields", result="ok",
        detail=f"Updated Contact {contact.record_id} for customer {customer_id}.",
    )
    db.commit()
    message = "Kontaktdaten wurden in Zoho CRM gespeichert." if contact.outputs.get("zoho_id") else "Kontaktdaten wurden im Hub gespeichert."
    query = urlencode({"fields": "success", "fields_message": message})
    return RedirectResponse(url=f"/customers/{customer_id}/contacts/{contact_id}?{query}#contact-fields", status_code=303)


@router.post("/customers/{customer_id}/contacts/{contact_id}/layout")
async def update_customer_contact_field_layout(
    customer_id: int,
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    user = _require_hub_admin(request)
    detail = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).get_contact_detail(customer_id=customer_id, contact_id=contact_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Contact not found for this customer.")
    try:
        _save_module_layout(request, db, CONTACT_FIELDS_LAYOUT_KEY, form)
    except (ModuleLayoutError, HubOperationError) as exc:
        db.rollback()
        query = urlencode({"layout": "error", "layout_message": str(exc)})
        return RedirectResponse(url=f"/customers/{customer_id}/contacts/{contact_id}?{query}#contact-fields", status_code=303)
    write_audit_log(
        db, site=None, actor=user.username, source="hub-web", action="update-contact-fields-layout", result="ok",
        detail="Updated the global contact field layout.",
    )
    db.commit()
    query = urlencode({"layout": "success", "layout_message": "Das globale Kontaktfelder-Layout wurde gespeichert."})
    return RedirectResponse(url=f"/customers/{customer_id}/contacts/{contact_id}?{query}#contact-fields", status_code=303)


@router.post("/customers/{customer_id}/contacts/{contact_id}/sync")
def sync_customer_contact(
    customer_id: int,
    contact_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        contact = _execute_contact_operation(
            db, user.username, "sync", customer_id=customer_id, contact_id=contact_id,
        )
    except ValueError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/customers/{customer_id}/contacts/{contact_id}?{query}#contact-fields", status_code=303)
    write_audit_log(
        db, site=None, actor=user.username, source="hub-web", action="sync-zoho-contact", result="ok",
        detail=f"Synchronized Contact {contact.record_id} for customer {customer_id}.",
    )
    db.commit()
    query = urlencode({"fields": "success", "fields_message": "Kontaktdaten wurden aus Zoho CRM aktualisiert."})
    return RedirectResponse(url=f"/customers/{customer_id}/contacts/{contact_id}?{query}#contact-fields", status_code=303)


@router.post("/customers/{customer_id}/link-site")
def link_customer_site(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[int, Form()],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        result = _website_gateway(request, db).execute("websites.link_customer", {"customer_id": str(customer_id), "site_id": str(site_id)})
        site = website_site(_website_gateway(request, db), site_id)
        customer = db.get(Customer, int(result.outputs["customer_id"]))
    except ValueError as exc:
        return RedirectResponse(
            url=f"/customers?{urlencode({'linked': 'error', 'message': str(exc)})}",
            status_code=303,
        )

    write_audit_log(
        db,
        site=site,
        actor=user.username,
        source="hub-web",
        action="link-zoho-customer-to-site",
        result="ok",
        detail=f"Linked Zoho customer {customer.name} ({customer.zoho_id}) after exact domain review.",
    )
    db.commit()
    return RedirectResponse(
        url=f"/customers?{urlencode({'linked': 'ok', 'customer': customer.name, 'site': site.domain})}",
        status_code=303,
    )


@router.get("/users", response_class=HTMLResponse)
def users_workbench_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    site_id: Annotated[list[int] | None, Query()] = None,
    site_scope: Literal["all", "selected"] = "selected",
    role: str = "all",
    customer_status: str = "all",
    fresh_users: str = "",
    active_refresh_run_id: Annotated[int | None, Query(ge=1)] = None,
    message: str = "",
    deletion_batch_id: Annotated[int | None, Query(ge=1)] = None,
    deletion_error: str = "",
):
    _require_hub_admin(request)
    selected_site_ids = set(site_id or []) if site_scope == "selected" else None
    return templates.TemplateResponse(
        request,
        "users.html",
        _user_workbench_context(
            request,
            db,
            query=q,
            site_ids=selected_site_ids,
            site_scope=site_scope,
            role=role,
            customer_status=customer_status,
            fresh_users=fresh_users,
            active_refresh_run_id=active_refresh_run_id,
            message=message,
            deletion_batch_id=deletion_batch_id,
            deletion_error=deletion_error,
        ),
    )


@router.post("/users/fresh-show")
def show_fresh_users(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Form()] = "",
    site_id: Annotated[list[int] | None, Form()] = None,
    site_scope: Annotated[Literal["all", "selected"], Form()] = "selected",
    role: Annotated[str, Form()] = "all",
    customer_status: Annotated[str, Form()] = "all",
    csrf_token: Annotated[str, Form()] = "",
):
    """Refresh only the WordPress user inventory for the selected sites."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    selected_site_ids = set(site_id or []) if site_scope == "selected" else set()
    scope_query: list[tuple[str, str | int]] = [("site_scope", "selected")]
    scope_query.extend(("site_id", selected_id) for selected_id in sorted(selected_site_ids))
    filter_query = [("q", q), ("role", role), ("customer_status", customer_status)]
    if not selected_site_ids:
        return RedirectResponse(
            url=f"/users?{urlencode(scope_query + filter_query + [('fresh_users', 'error'), ('message', 'Select at least one site before refreshing users.')])}",
            status_code=303,
        )
    run, created = _start_shared_fleet(request, db, FleetRefreshService.MODE_FRESH_USERS, selected_site_ids)
    db.commit()
    if created:
        background_tasks.add_task(FleetRefreshService.process_run, run.id)
    elif run.mode not in FleetRefreshService.user_modes():
        return RedirectResponse(
            url=f"/users?{urlencode(scope_query + filter_query + [('fresh_users', 'error'), ('message', 'Another refresh is already running. Please wait until it finishes.')])}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/users?{urlencode(scope_query + filter_query + [('fresh_users', 'running'), ('active_refresh_run_id', run.id)])}",
        status_code=303,
    )


@router.get("/backups", response_class=HTMLResponse)
def backup_workbench_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[list[int] | None, Query()] = None,
    site_scope: Literal["all", "selected"] = "selected",
    fresh_backups: str = "",
    active_refresh_run_id: Annotated[int | None, Query(ge=1)] = None,
    message: str = "",
):
    _require_hub_admin(request)
    selected_site_ids = set(site_id or []) if site_scope == "selected" else None
    return templates.TemplateResponse(
        request,
        "backups.html",
        _backup_workbench_context(
            request,
            db,
            site_ids=selected_site_ids,
            site_scope=site_scope,
            fresh_backups=fresh_backups,
            active_refresh_run_id=active_refresh_run_id,
            message=message,
        ),
    )


@router.post("/backups/fresh-show")
def show_fresh_backups(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[list[int] | None, Form()] = None,
    site_scope: Annotated[Literal["all", "selected"], Form()] = "selected",
    csrf_token: Annotated[str, Form()] = "",
):
    """Refresh only the read-only backup status for the selected sites."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    selected_site_ids = set(site_id or []) if site_scope == "selected" else set()
    scope_query: list[tuple[str, str | int]] = [("site_scope", "selected")]
    scope_query.extend(("site_id", selected_id) for selected_id in sorted(selected_site_ids))
    if not selected_site_ids:
        return RedirectResponse(
            url=f"/backups?{urlencode(scope_query + [('fresh_backups', 'error'), ('message', 'Select at least one site before refreshing backup status.')])}",
            status_code=303,
        )
    run, created = _start_shared_fleet(request, db, FleetRefreshService.MODE_FRESH_BACKUPS, selected_site_ids)
    db.commit()
    if created:
        background_tasks.add_task(FleetRefreshService.process_run, run.id)
    elif run.mode not in FleetRefreshService.backup_modes():
        return RedirectResponse(
            url=f"/backups?{urlencode(scope_query + [('fresh_backups', 'error'), ('message', 'Another refresh is already running. Please wait until it finishes.')])}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/backups?{urlencode(scope_query + [('fresh_backups', 'running'), ('active_refresh_run_id', run.id)])}",
        status_code=303,
    )


@router.post("/users/bulk/create", response_class=HTMLResponse)
def create_selected_site_users(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[list[int] | None, Form()] = None,
    username: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "subscriber",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        outcomes = _wordpress_remote(request, db, "wordpress.users.bulk_create",
            site_ids=site_id or [],
            username=username,
            email=email,
            password=password,
            role=role,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return _render_user_workbench(request, db, error=str(exc))
    return _render_user_workbench(request, db, outcomes=outcomes, action_label="User creation")


@router.post("/users/bulk/role", response_class=HTMLResponse)
def update_selected_user_roles(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    selected: Annotated[list[str] | None, Form()] = None,
    role: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        outcomes = _wordpress_remote(request, db, "wordpress.users.bulk_role",
            selected_keys=selected or [],
            role=role,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return _render_user_workbench(request, db, error=str(exc))
    return _render_user_workbench(request, db, outcomes=outcomes, action_label="Role update")


@router.post("/users/bulk/password", response_class=HTMLResponse)
def update_selected_user_passwords(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    selected: Annotated[list[str] | None, Form()] = None,
    password: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        outcomes = _wordpress_remote(request, db, "wordpress.users.bulk_password",
            selected_keys=selected or [],
            password=password,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return _render_user_workbench(request, db, error=str(exc))
    return _render_user_workbench(request, db, outcomes=outcomes, action_label="Password update")


@router.post("/users/actions/create-site", response_class=JSONResponse)
def create_user_on_one_site(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[int, Form()],
    username: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
):
    """Execute one browser-orchestrated creation without storing the submitted password."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    site = SiteRepository(db).get_site(site_id)
    if site is None:
        return {"site_id": site_id, "site": "Unknown site", "username": username, "status": "failed", "message": "The selected site no longer exists."}
    try:
        created = _wordpress_remote(request, db, "wordpress.users.create",
            site_id=site_id,
            username=username,
            email=email,
            password=password,
            role=role,
            display_name="",
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return {"site_id": site.id, "site": site.domain, "username": username, "status": "failed", "message": str(exc)}
    return {
        "site_id": site.id,
        "site": site.domain,
        "username": str(created.get("username") or username),
        "status": "succeeded",
        "message": "Created and verified by WordPress.",
    }


@router.post("/users/actions/update-role", response_class=JSONResponse)
def update_one_user_role(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[int, Form()],
    user_id: Annotated[int, Form()],
    role: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
):
    """Execute one browser-orchestrated role change."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    site = SiteRepository(db).get_site(site_id)
    if site is None:
        return {"site_id": site_id, "site": "Unknown site", "username": str(user_id), "status": "failed", "message": "The selected site no longer exists."}
    try:
        changed = _wordpress_remote(request, db, "wordpress.users.role",
            site_id=site_id,
            user_id=user_id,
            role=role,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return {"site_id": site.id, "site": site.domain, "username": str(user_id), "status": "failed", "message": str(exc)}
    return {
        "site_id": site.id,
        "site": site.domain,
        "username": str(changed.get("username") or user_id),
        "status": "succeeded",
        "message": f"Role changed to {role} and verified by WordPress.",
    }


@router.post("/users/actions/update-password", response_class=JSONResponse)
def update_one_user_password(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[int, Form()],
    user_id: Annotated[int, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
):
    """Execute one browser-orchestrated password change without storing the password."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    site = SiteRepository(db).get_site(site_id)
    if site is None:
        return {"site_id": site_id, "site": "Unknown site", "username": str(user_id), "status": "failed", "message": "The selected site no longer exists."}
    try:
        changed = _wordpress_remote(request, db, "wordpress.users.password",
            site_id=site_id,
            user_id=user_id,
            password=password,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return {"site_id": site.id, "site": site.domain, "username": str(user_id), "status": "failed", "message": str(exc)}
    return {
        "site_id": site.id,
        "site": site.domain,
        "username": str(changed.get("username") or user_id),
        "status": "succeeded",
        "message": "Password changed and verified by WordPress.",
    }


@router.post("/users/bulk/delete/prepare")
def prepare_selected_user_deletions(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    selected: Annotated[list[str] | None, Form()] = None,
    deletion_confirmation: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "/users",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    return_url = _safe_users_return_url(return_to)
    try:
        batch = _wordpress_remote(request, db, "wordpress.users.deletion_prepare",
            selected_keys=selected or [],
            actor=user.username,
            deletion_confirmation=deletion_confirmation,
        )
        db.commit()
    except ValueError as exc:
        return RedirectResponse(
            url=_users_return_url_with_deletion_error(return_url, str(exc)),
            status_code=303,
        )
    return RedirectResponse(url=_user_deletion_batch_url(batch.id, return_to=return_url), status_code=303)


@router.post("/users/deletion-batches/{batch_id}/start")
def start_selected_user_deletion_batch(
    batch_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    item_id: Annotated[list[int] | None, Form()] = None,
    reassign_to_user_id: Annotated[list[int] | None, Form()] = None,
    return_to: Annotated[str, Form()] = "/users",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    _require_hub_admin(request)
    return_url = _safe_users_return_url(return_to)
    try:
        _wordpress_remote(request, db, "wordpress.users.deletion_start",
            batch_id=batch_id,
            item_ids=item_id or [],
            replacement_user_ids=reassign_to_user_id or [],
        )
        db.commit()
    except ValueError as exc:
        return RedirectResponse(url=_user_deletion_batch_url(batch_id, return_to=return_url, error=str(exc)), status_code=303)
    schedule_pending_user_deletions()
    return RedirectResponse(url=_user_deletion_batch_url(batch_id, return_to=return_url), status_code=303)


@router.get("/users/deletion-batches/{batch_id}/status", response_class=JSONResponse)
def user_deletion_batch_status(
    batch_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    service = UserDeletionBatchService(db=db, cipher=get_secret_cipher())
    try:
        batch = deletion_batch(_website_gateway(request, db), batch_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service.status_payload(batch)


@router.post("/users/deletion-batches/{batch_id}/cancel")
def cancel_selected_user_deletion_batch(
    batch_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    return_to: Annotated[str, Form()] = "/users",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    _require_hub_admin(request)
    return_url = _safe_users_return_url(return_to)
    try:
        service = UserDeletionBatchService(db=db, cipher=get_secret_cipher())
        batch = service.get_batch(batch_id)
        was_prepared = batch is not None and batch.status == UserDeletionBatchService.BATCH_PREPARED
        _wordpress_remote(request, db, "wordpress.users.deletion_cancel", batch_id=batch_id)
        db.commit()
    except ValueError as exc:
        return RedirectResponse(url=_user_deletion_batch_url(batch_id, return_to=return_url, error=str(exc)), status_code=303)
    if was_prepared:
        return RedirectResponse(url=return_url, status_code=303)
    return RedirectResponse(url=_user_deletion_batch_url(batch_id, return_to=return_url), status_code=303)


@router.get("/updates", response_class=HTMLResponse)
def update_workbench_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    site_id: Annotated[list[int] | None, Query()] = None,
    site_scope: Literal["all", "selected"] = "selected",
    plugin: str = "",
    kind: Literal["all", "wordpress", "plugin", "theme"] = "all",
    activity: Literal["all", "active", "inactive"] = "all",
    diagnosis: Literal[
        "all",
        "attention",
        "update-ready",
        "aligned",
        "provider-conflict",
        "provider-package-unavailable",
        "site-offer-missing",
        "crocoblock-license-step",
        "crocoblock-offer-missing",
        "site-newer-than-reference",
        "official-unavailable",
        "not-checked",
    ] = "all",
    update_batch: str = "",
    complete_update_run_id: Annotated[int | None, Query(ge=1)] = None,
    direct_update: str = "",
    fresh_updates: str = "",
    message: str = "",
    view: Literal["updates", "refresh-protocol"] = "updates",
    refresh_run_id: Annotated[int | None, Query(ge=1)] = None,
    active_refresh_run_id: Annotated[int | None, Query(ge=1)] = None,
):
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    selected_site_ids = set(site_id or []) if site_scope == "selected" else None
    all_items, entries, filtered_entries, workbench_summary = update_workbench(
        _website_gateway(request, db),
        query=q,
        kind=kind,
        activity=activity,
        diagnosis=diagnosis,
        site_ids=selected_site_ids,
        plugin_identifier=plugin,
    )


    matching_items = inventory_service.filter_items(all_items, query=q) if q.strip() else all_items
    if selected_site_ids is not None:
        matching_items = [item for item in matching_items if item.site.id in selected_site_ids]
    if plugin:
        matching_items = [
            item
            for item in matching_items
            if any(str(item_plugin.get("plugin_file", "")).strip() == plugin for item_plugin in item.plugins)
        ]
    matching_sites = [item.site for item in matching_items]
    site_options = sorted((item.site for item in all_items), key=lambda site: site.domain.casefold())
    plugin_options = sorted(
        {
            (entry.identifier, entry.name)
            for entry in entries
            if entry.kind == "plugin" and entry.identifier
        },
        key=lambda option: (option[1].casefold(), option[0].casefold()),
    )
    maintenance_service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
    try:
        batch_runs = maintenance_batch(_website_gateway(request, db), update_batch)
        complete_site_update_run = complete_run(_website_gateway(request, db), complete_update_run_id) if complete_update_run_id is not None else None
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    batch_running = any(run.status == "running" for run in batch_runs)
    if batch_running:
        # Resume a user-started batch if a process restart interrupted polling.
        schedule_pending_direct_updates()
    complete_site_update_running = (
        complete_site_update_run is not None
        and complete_site_update_run.status == "running"
    )
    if complete_site_update_running:
        schedule_pending_complete_site_updates()
    fleet_refresh_service = FleetRefreshService(db=db)
    is_admin = getattr(request.state.hub_user, "role", None) == "admin"
    active_fleet_refresh_run = fleet_refresh_service.get_active_run(modes=FleetRefreshService.update_modes()) if is_admin else None
    progress_refresh_run = (
        fleet_refresh_service.get_run(active_refresh_run_id)
        if active_refresh_run_id is not None and is_admin
        else active_fleet_refresh_run
    )
    if progress_refresh_run is not None and progress_refresh_run.mode not in FleetRefreshService.update_modes():
        progress_refresh_run = active_fleet_refresh_run
    fresh_completion_url = ""
    if (
        fresh_updates == "running"
        and progress_refresh_run is not None
        and progress_refresh_run.mode == FleetRefreshService.MODE_FRESH_UPDATES
    ):
        completion_scope_query: list[tuple[str, str | int]] = [("site_scope", "selected")]
        completion_scope_query.extend(("site_id", selected_id) for selected_id in sorted(selected_site_ids or []))
        completion_filter_query = [
            ("q", q),
            ("plugin", plugin),
            ("kind", kind),
            ("activity", activity),
            ("diagnosis", diagnosis),
            ("fresh_updates", "ok"),
            ("message", "Fresh update checks completed. The table now shows the current results."),
        ]
        fresh_completion_url = f"/updates?{urlencode(completion_scope_query + completion_filter_query)}"
    refresh_runs = fleet_history(_website_gateway(request, db), modes=FleetRefreshService.update_history_modes()) if is_admin else []
    selected_refresh_run = next((run for run in refresh_runs if run.id == refresh_run_id), None)
    refresh_site_results = (
        fleet_refresh_service.list_site_results(run_id=selected_refresh_run.id)
        if selected_refresh_run is not None
        else []
    )
    refresh_result_sites = {item.site.id: item.site for item in all_items}
    return templates.TemplateResponse(
        request,
        "updates.html",
        {
            "entries": filtered_entries,
            "summary": workbench_summary,
            "filters": {
                "q": q,
                "site_ids": sorted(selected_site_ids or []),
                "site_scope": site_scope,
                "plugin": plugin,
                "kind": kind,
                "activity": activity,
                "diagnosis": diagnosis,
            },
            "site_options": site_options,
            "site_selector": build_site_selector_context(
                action="/updates",
                form_id="update-site-scope-form",
                sites=site_options,
                selected_site_ids=selected_site_ids,
                site_scope=site_scope,
                submit_label="Start",
                target_form_id="complete-site-update-form",
                csrf_token=get_csrf_token(request),
                secondary_submit_action="/updates/fresh-show",
                secondary_primary_label="Gespeicherte Updates anzeigen",
                secondary_submit_label="Frische Updates prüfen",
                protocol_submit_label="Aktualisierungsprotokoll anzeigen",
                selected_display_mode="protocol" if view == "refresh-protocol" else "stored",
            ),
            "plugin_options": plugin_options,
            "csrf_token": get_csrf_token(request),
            "matching_sites": matching_sites,
            "update_batch": update_batch if batch_runs else "",
            "batch_runs": batch_runs,
            "batch_running": batch_running,
            "batch_cancellable": maintenance_service.direct_update_batch_can_be_cancelled(batch_runs),
            "batch_cancellation_requested": maintenance_service.direct_update_batch_cancellation_requested(batch_runs),
            "complete_site_update_run": complete_site_update_run,
            "complete_site_update_running": complete_site_update_running,
            "direct_update_cancel_return_url": str(request.url.path)
            + (f"?{request.url.query}" if request.url.query else ""),
            "show_update_selection": not batch_runs and complete_site_update_run is None and view != "refresh-protocol",
            "direct_update": direct_update,
            "fresh_updates": fresh_updates,
            "active_fleet_refresh_run": active_fleet_refresh_run,
            "progress_refresh_run": progress_refresh_run,
            "fresh_completion_url": fresh_completion_url,
            "show_refresh_protocol": view == "refresh-protocol",
            "refresh_runs": refresh_runs,
            "selected_refresh_run": selected_refresh_run,
            "refresh_site_results": refresh_site_results,
            "refresh_result_sites": refresh_result_sites,
            "message": message,
        },
    )


@router.post("/updates/fresh-show")
def show_fresh_updates(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Form()] = "",
    site_id: Annotated[list[int] | None, Form()] = None,
    site_scope: Annotated[Literal["all", "selected"], Form()] = "selected",
    plugin: Annotated[str, Form()] = "",
    kind: Annotated[Literal["all", "wordpress", "plugin", "theme"], Form()] = "all",
    activity: Annotated[Literal["all", "active", "inactive"], Form()] = "all",
    diagnosis: Annotated[
        Literal[
            "all",
            "attention",
            "update-ready",
            "aligned",
            "provider-conflict",
            "provider-package-unavailable",
            "site-offer-missing",
            "crocoblock-license-step",
            "crocoblock-offer-missing",
            "site-newer-than-reference",
            "official-unavailable",
            "not-checked",
        ],
        Form(),
    ] = "all",
    csrf_token: Annotated[str, Form()] = "",
):
    """Queue the explicit fresh-data alternative for Show updates."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    selected_site_ids = set(site_id or []) if site_scope == "selected" else set()
    scope_query: list[tuple[str, str | int]] = [("site_scope", "selected")]
    scope_query.extend(("site_id", selected_id) for selected_id in sorted(selected_site_ids))
    filter_query = [("q", q), ("plugin", plugin), ("kind", kind), ("activity", activity), ("diagnosis", diagnosis)]
    if not selected_site_ids:
        return RedirectResponse(
            url=f"/updates?{urlencode(scope_query + filter_query + [('fresh_updates', 'error'), ('message', 'Select at least one site before loading fresh updates.')])}",
            status_code=303,
        )
    run, created = _start_shared_fleet(request, db, FleetRefreshService.MODE_FRESH_UPDATES, selected_site_ids)
    db.commit()
    if created:
        background_tasks.add_task(FleetRefreshService.process_run, run.id)
    elif run.mode not in FleetRefreshService.update_modes():
        return RedirectResponse(
            url=f"/updates?{urlencode(scope_query + filter_query + [('fresh_updates', 'error'), ('message', 'Another refresh is already running. Please wait until it finishes.')])}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/updates?{urlencode(scope_query + filter_query + [('fresh_updates', 'running'), ('active_refresh_run_id', run.id)])}",
        status_code=303,
    )


@router.get("/updates/refresh-runs/{run_id}/status", response_class=JSONResponse)
def fleet_refresh_run_status(
    run_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Return the persisted live progress for one refresh without reloading the workbench."""
    _require_hub_admin(request)
    try:
        return fleet_status(_website_gateway(request, db), run_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/updates/direct-update-batches/{batch_id}/status", response_class=JSONResponse)
def direct_update_batch_status(
    batch_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    try:
        return batch_status(_website_gateway(request, db), batch_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/updates/direct-update-batches/{batch_id}/cancel")
def cancel_direct_update_batch(
    batch_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "/updates",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    return_url = _safe_updates_return_url(return_to)

    try:
        outcome = _wordpress_remote(request, db, "wordpress.updates.cancel_batch",
            batch_id=batch_id,
            actor=user.username,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            url=_updates_return_url_with_message(return_url, str(exc)),
            status_code=303,
        )

    if outcome.cancelled_queued_runs:
        message = (
            f"Cancellation was requested. {outcome.cancelled_queued_runs} queued update"
            f"{'s were' if outcome.cancelled_queued_runs != 1 else ' was'} cancelled."
        )
        if outcome.processing_runs:
            message += " Already running updates will finish safely."
    elif outcome.processing_runs:
        message = "Cancellation was already requested. Already running updates will finish safely."
    else:
        message = "This direct update batch had already finished."
    return RedirectResponse(
        url=_updates_return_url_with_message(return_url, message),
        status_code=303,
    )


@router.get("/updates/complete-site-update-runs/{run_id}/status", response_class=JSONResponse)
def complete_site_update_run_status(
    run_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    try:
        return complete_status(_website_gateway(request, db), run_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/updates/complete-site-update-runs/{run_id}/cancel")
def cancel_complete_site_update_run(
    run_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "/updates",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    return_url = _safe_updates_return_url(return_to)
    try:
        cancelled = _wordpress_remote(request, db, "wordpress.updates.cancel_complete",
            run_id=run_id,
            actor=user.username,
        )
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            url=_updates_return_url_with_message(return_url, str(exc)),
            status_code=303,
        )
    message = (
        "Cancellation was requested. The current component update will finish safely."
        if cancelled
        else "This complete update workflow had already finished or was already being cancelled."
    )
    return RedirectResponse(
        url=_updates_return_url_with_message(return_url, message),
        status_code=303,
    )


@router.get("/plugin-installations", response_class=HTMLResponse)
def plugin_installations_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[list[int] | None, Query()] = None,
    site_scope: Literal["all", "selected"] = "selected",
    install_batch: str = "",
    plugin_install: str = "",
    message: str = "",
):
    site_options = website_sites(_website_gateway(request, db))
    selected_site_ids = set(site_id or []) if site_scope == "selected" else None
    maintenance_service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
    try:
        batch_runs = maintenance_batch(_website_gateway(request, db), install_batch, installation=True)
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if any(run.status == "running" for run in batch_runs):
        schedule_pending_direct_updates()
    return templates.TemplateResponse(
        request,
        "plugin_installations.html",
        {
            "site_selector": build_site_selector_context(
                action="/plugin-installations",
                form_id="plugin-install-site-scope-form",
                target_form_id="plugin-install-form",
                sites=site_options,
                selected_site_ids=selected_site_ids,
                site_scope=site_scope,
                submit_label="Installieren",
                submit_behavior="install",
            ),
            "install_batch": install_batch if batch_runs else "",
            "batch_runs": batch_runs,
            "plugin_install": plugin_install,
            "message": message,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/plugin-installations/catalog")
def plugin_installation_catalog(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    search: str = "",
    browse: str = "popular",
    page: Annotated[int, Query(ge=1, le=100)] = 1,
):
    try:
        catalog = plugin_catalog(_website_gateway(request, db), {"search": search, "browse": browse, "page": str(page)})
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PluginPackageError as exc:
        return JSONResponse(status_code=502, content={"error": str(exc)})

    return {
        "items": [
            {
                "slug": item.slug,
                "name": item.name,
                "short_description": item.short_description,
                "version": item.version,
                "rating": item.rating,
                "num_ratings": item.num_ratings,
                "active_installs": item.active_installs,
                "last_updated": item.last_updated,
                "requires": item.requires,
                "tested": item.tested,
                "icon_url": item.icon_url,
            }
            for item in catalog.items
        ],
        "page": catalog.page,
        "pages": catalog.pages,
        "total": catalog.total,
    }


@router.post("/plugin-installations/queue")
async def queue_plugin_installation(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    source: Annotated[Literal["wordpress-org", "zip-upload"], Form()] = "wordpress-org",
    wordpress_org_slug: Annotated[str, Form()] = "",
    package_zip: Annotated[UploadFile | None, File()] = None,
    site_scope: Annotated[Literal["all", "selected"], Form()] = "selected",
    site_id: Annotated[list[int] | None, Form()] = None,
    activate: Annotated[bool, Form()] = False,
    replace_existing: Annotated[bool, Form()] = False,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    selected_site_ids = site_id or []
    if site_scope == "all":
        selected_site_ids = [site.id for site in SiteRepository(db).list_sites(limit=1000)]

    packages = PluginInstallationPackageService(db=db)
    try:
        if source == "wordpress-org":
            package = packages.prepare_wordpress_org_plugin(slug=wordpress_org_slug)
        else:
            if package_zip is None or not package_zip.filename:
                raise PluginPackageError("Choose a ZIP file before queueing the installation.")
            package = packages.prepare_uploaded_zip(filename=package_zip.filename, source=package_zip.file)
        outcome = _wordpress_remote(request, db, "wordpress.plugins.install",
            site_ids=selected_site_ids,
            package_id=package.id,
            activate=activate,
            replace_existing=replace_existing,
            actor=user.username,
        )
    except (PluginPackageError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/plugin-installations?{urlencode({'plugin_install': 'error', 'message': str(exc)})}",
            status_code=303,
        )
    finally:
        if package_zip is not None:
            await package_zip.close()

    schedule_pending_direct_updates()
    return RedirectResponse(
        url=f"/plugin-installations?{urlencode({'install_batch': outcome.batch_id, 'plugin_install': 'started', 'message': outcome.message})}",
        status_code=303,
    )


@router.post("/updates/apply-action")
def apply_update_workbench_action(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    maintenance_action: Annotated[Literal["direct-updates"], Form()],
    selected: Annotated[list[str] | None, Form()] = None,
    large_batch_confirmation: Annotated[str, Form()] = "",
    site_scope: Annotated[Literal["all", "selected"], Form()] = "selected",
    site_id: Annotated[list[int] | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    selected_site_ids = set(site_id or []) if site_scope == "selected" else None
    scope_query: list[tuple[str, str | int]] = [("site_scope", site_scope)]
    if selected_site_ids is not None:
        scope_query.extend(("site_id", selected_id) for selected_id in sorted(selected_site_ids))

    try:
        outcome = _wordpress_remote(request, db, "wordpress.updates.start",
            selected_keys=selected or [],
            actor=user.username,
            large_batch_confirmation=large_batch_confirmation,
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/updates?{urlencode(scope_query + [('direct_update', 'error'), ('message', str(exc))])}",
            status_code=303,
        )
    schedule_pending_direct_updates()
    return RedirectResponse(
        url=f"/updates?{urlencode(scope_query + [('update_batch', outcome.batch_id), ('direct_update', 'started'), ('message', outcome.message)])}",
        status_code=303,
    )


@router.post("/updates/plugin-auto-updates")
def set_plugin_auto_updates(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[list[int] | None, Form()] = None,
    plugin_file: Annotated[list[str] | None, Form()] = None,
    blocked: Annotated[Literal["true", "false"], Form()] = "true",
    confirmed: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    _require_hub_admin(request)
    if confirmed != "yes":
        raise HTTPException(status_code=400, detail="Bitte Websites und Plugins zuerst bestaetigen.")
    try:
        result = _website_gateway(request, db).execute("wordpress.plugins.auto_updates", {
            "site_ids": json.dumps(site_id or []), "plugin_files": json.dumps(plugin_file or []), "blocked": blocked,
        })
        db.commit()
    except HubOperationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(result.href, status_code=303)


@router.post("/updates/complete-site-update")
def start_complete_site_update(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_scope: Annotated[Literal["all", "selected"], Form()] = "selected",
    site_id: Annotated[list[int] | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    selected_site_ids = sorted(set(site_id or [])) if site_scope == "selected" else []
    if len(selected_site_ids) != 1:
        return RedirectResponse(
            url=(
                "/updates?"
                + urlencode(
                    [
                        ("site_scope", "selected"),
                        ("complete_update", "error"),
                        ("message", "Select exactly one website to start the complete update workflow."),
                    ]
                )
            ),
            status_code=303,
        )

    site_id_value = selected_site_ids[0]
    scope_query: list[tuple[str, str | int]] = [("site_scope", "selected"), ("site_id", site_id_value)]
    try:
        outcome = _wordpress_remote(request, db, "wordpress.updates.complete",
            site_id=site_id_value,
            actor=user.username,
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/updates?{urlencode(scope_query + [('complete_update', 'error'), ('message', str(exc))])}",
            status_code=303,
        )

    schedule_pending_complete_site_updates()
    return RedirectResponse(
        url=(
            f"/updates?{urlencode(scope_query + [('complete_update_run_id', outcome.run.id), ('complete_update', 'started'), ('message', outcome.message)])}"
        ),
        status_code=303,
    )


@router.post("/updates/refresh-runs/{run_id}/cancel")
def cancel_fleet_refresh_run(
    run_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "/updates",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    if return_to not in {"/updates", "/users", "/backups"}:
        return_to = "/updates"
    service = FleetRefreshService(db=db)
    try:
        outcome = _website_gateway(request, db).execute("wordpress.fleet.cancel", {"run_id": str(run_id)})
        run = fleet_run(_website_gateway(request, db), outcome.record_id)
        cancelled = outcome.outputs["cancelled"] == "true"
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            url=f"{return_to}?{urlencode({'message': str(exc)})}",
            status_code=303,
        )

    message = (
        "Cancellation was requested. Current site checks will finish, but no further sites or provider checks will start."
        if cancelled and run.status == "cancelling"
        else "The queued refresh was cancelled."
        if cancelled
        else "This fleet refresh had already finished."
    )
    return RedirectResponse(
        url=f"{return_to}?{urlencode({'active_refresh_run_id': run.id, 'message': message})}",
        status_code=303,
    )


@router.post("/updates/execute-selected-plugins")
@router.post("/updates/execute-selected-updates")
def execute_selected_plugin_updates(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    selected: Annotated[list[str] | None, Form()] = None,
    site_id: Annotated[int | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
    redirect_path = f"/sites/{site_id}" if site_id is not None else "/updates"
    try:
        if site_id is None:
            outcome = _wordpress_remote(request, db, "wordpress.updates.start", selected_keys=selected or [])
        else:
            outcome = _wordpress_remote(request, db, "wordpress.updates.site", site_id=site_id, selected_keys=selected or [])
    except ValueError as exc:
        return RedirectResponse(
            url=f"{redirect_path}?{urlencode({'direct_update': 'error', 'message': str(exc)})}",
            status_code=303,
        )
    schedule_pending_direct_updates()
    return RedirectResponse(
        url=f"{redirect_path}?{urlencode({'update_batch': outcome.batch_id, 'direct_update': 'started', 'message': outcome.message})}",
        status_code=303,
    )


@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail_page(site_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    try:
        item = website_inventory(_website_gateway(request, db), site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    site = item.site
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    maintenance_run_history = MaintenanceRunService(db=db, cipher=get_secret_cipher()).list_site_run_history(site_id)
    current_user = getattr(request.state, "hub_user", None)
    user_inventory = users_inventory(_website_gateway(request, db), site_id) if current_user and current_user.role == "admin" else None
    site_entries = [
        entry
        for entry in inventory_service.build_update_workbench([item])
        if entry.site.id == site.id
    ]
    return templates.TemplateResponse(
        request,
        "site_detail.html",
        {
            "site": site,
            "update_entries": site_entries,
            "csrf_token": get_csrf_token(request),
            "maintenance_run_history": maintenance_run_history,
            "user_inventory": user_inventory,
            "removable_test_registration": _is_removable_empty_test_registration(site),
        },
    )


def _website_gateway(request, db):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username)


def _wordpress_remote(request, db, key, **values):
    return execute_ui_remote(_website_gateway(request, db), key, **values)


def _start_shared_fleet(request, db, mode, site_ids):
    service = _website_gateway(request, db)
    try:
        outcome = service.execute("wordpress.fleet.start", {"mode": mode, "site_ids": json.dumps(sorted(site_ids))})
        return fleet_run(service, outcome.record_id), outcome.outputs["created"] == "true"
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/wordpress/jobs/{job_id}", response_class=HTMLResponse)
def wordpress_job_page(job_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    try:
        job = _website_gateway(request, db).query("wordpress.jobs.read", {"job_id": str(job_id)})
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(request, "wordpress_job.html", {"job": job, "csrf_token": get_csrf_token(request)})


@router.post("/wordpress/jobs/{job_id}/cancel")
def cancel_wordpress_job(job_id: int, request: Request, db: Annotated[Session, Depends(get_db)], csrf_token: Annotated[str, Form()] = ""):
    require_csrf(request, csrf_token)
    try:
        result = _website_gateway(request, db).execute("wordpress.jobs.cancel", {"job_id": str(job_id)})
        db.commit()
    except HubOperationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(result.href, status_code=303)


@router.post("/sites/{site_id}/refresh")
def refresh_site_from_detail(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    repository = SiteRepository(db)
    site = repository.get_site(site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")

    try:
        state_payload = _wordpress_remote(request, db, "wordpress.inventory.refresh", site_id=site_id)
        updates_payload = _wordpress_remote(request, db, "wordpress.updates.refresh", site_id=site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        write_audit_log(
            db,
            site=site,
            actor=user.username,
            source="hub-web",
            action="request-site-refresh",
            result="error",
            detail=exc.message,
        )
        db.commit()
        return RedirectResponse(
            url=f"/sites/{site_id}?{urlencode({'refresh': 'error', 'message': exc.message})}",
            status_code=303,
        )

    summary = updates_payload["snapshot"].summary_json
    update_count = int(summary.get("total", 0)) if isinstance(summary, dict) else 0
    write_audit_log(
        db,
        site=site,
        actor=user.username,
        source="hub-web",
        action="request-site-refresh",
        result="ok",
        detail=(
            f"Stored current state from {state_payload['refreshed_at']} and "
            f"{update_count} available updates."
        ),
    )
    db.commit()
    return RedirectResponse(url=f"/sites/{site_id}?refresh=ok", status_code=303)


@router.post("/sites/{site_id}/backup-refresh")
def refresh_site_backup_from_detail(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    repository = SiteRepository(db)
    site = repository.get_site(site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")

    try:
        payload = _wordpress_remote(request, db, "wordpress.backups.refresh", site_id=site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        write_audit_log(
            db,
            site=site,
            actor=user.username,
            source="hub-web",
            action="request-site-backup-refresh",
            result="error",
            detail=exc.message,
        )
        db.commit()
        return RedirectResponse(
            url=f"/sites/{site_id}?{urlencode({'backup_refresh': 'error', 'message': exc.message})}",
            status_code=303,
        )

    snapshot = payload["snapshot"]
    write_audit_log(
        db,
        site=site,
        actor=user.username,
        source="hub-web",
        action="request-site-backup-refresh",
        result="ok",
        detail=(
            f"Stored read-only backup status: available={snapshot.backup_available}, "
            f"complete={snapshot.backup_complete}."
        ),
    )
    db.commit()
    return RedirectResponse(url=f"/sites/{site_id}?backup_refresh=ok", status_code=303)


@router.post("/sites/{site_id}/backup-actions")
def execute_site_backup_action_from_detail(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    backup_action: Annotated[
        Literal["create-and-prune-oldest", "create-complete", "delete-selected", "check-backups"],
        Form(),
    ],
    selected_backup: Annotated[list[str] | None, Form()] = None,
    deletion_confirmation: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
    try:
        if backup_action == "check-backups":
            snapshot = _wordpress_remote(request, db, "wordpress.backups.refresh", site_id=site_id)["snapshot"]
            message = (
                f"UpdraftPlus backup list checked: {snapshot.backup_count} backup set(s) are currently reported. "
                "No backup was created, changed, or deleted."
            )
            result = "ok"
        elif backup_action == "delete-selected":
            outcome = _wordpress_remote(request, db, "wordpress.backups.delete",
                site_id=site_id,
                selections=selected_backup or [],
                actor=user.username,
                deletion_confirmation=deletion_confirmation,
            )
            message = outcome.message
            result = outcome.result
        else:
            outcome = _wordpress_remote(request, db, "wordpress.backups.create",
                site_id=site_id,
                actor=user.username,
                cleanup_oldest=backup_action == "create-and-prune-oldest",
            )
            message = outcome.message
            result = outcome.result
    except (SiteMcpProxyError, ValueError) as exc:
        result = "error"
        message = str(exc)

    return RedirectResponse(
        url=f"/sites/{site_id}?{urlencode({'backup_action': result, 'message': message})}#backups",
        status_code=303,
    )


@router.post("/sites/{site_id}/users/refresh")
def refresh_site_users_from_detail(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        inventory = _wordpress_remote(request, db, "wordpress.users.refresh", site_id=site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        return _site_users_redirect(site_id, "error", f"User inventory refresh failed: {exc.message}")
    if inventory.snapshot.available:
        return _site_users_redirect(site_id, "ok", f"Stored {inventory.snapshot.user_count} WordPress user(s).")
    return _site_users_redirect(site_id, "unsupported", inventory.snapshot.message or "User inventory is not available on this Bridge version.")


@router.post("/sites/{site_id}/users")
def create_site_user_from_detail(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "subscriber",
    password: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        created = _wordpress_remote(request, db, "wordpress.users.create",
            site_id=site_id,
            username=username,
            email=email,
            password=password,
            role=role,
            display_name=display_name,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return _site_users_redirect(site_id, "error", f"User was not created: {str(exc)}")
    return _site_users_redirect(site_id, "ok", f"WordPress user {created['username']} was created.")


@router.post("/sites/{site_id}/users/{user_id}/password")
def update_site_user_password_from_detail(
    site_id: int,
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        changed = _wordpress_remote(request, db, "wordpress.users.password",
            site_id=site_id,
            user_id=user_id,
            password=password,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return _site_users_redirect(site_id, "error", f"Password was not changed: {str(exc)}")
    return _site_users_redirect(site_id, "ok", f"Password changed for WordPress user {changed['username']}.")


@router.post("/sites/{site_id}/users/{user_id}/delete")
def delete_site_user_from_detail(
    site_id: int,
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    reassign_to_user_id: Annotated[int, Form()] = 0,
    confirmation_username: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        _wordpress_remote(request, db, "wordpress.users.delete",
            site_id=site_id,
            user_id=user_id,
            reassign_to_user_id=reassign_to_user_id,
            confirmed_username=confirmation_username,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return _site_users_redirect(site_id, "error", f"User was not deleted: {str(exc)}")
    return _site_users_redirect(site_id, "ok", "WordPress user was deleted and their content was reassigned.")


@router.post("/sites/{site_id}/maintenance/updraftplus-backup")
def start_updraftplus_backup_from_detail(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        outcome = _wordpress_remote(request, db, "wordpress.backups.create",
            site_id=site_id,
            actor=user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(
        url=f"/sites/{site_id}?{urlencode({'maintenance': outcome.result, 'message': outcome.message})}",
        status_code=303,
    )


@router.post("/sites/{site_id}/remove-empty-test-registration")
def remove_empty_test_registration(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        outcome = _website_gateway(request, db).execute("websites.remove_test_registration", {"site_id": str(site_id)})
    except HubOperationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(url=outcome.href, status_code=303)


def _is_removable_empty_test_registration(site) -> bool:
    return is_removable_empty_test_registration(site)


def _safe_users_return_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path != "/users":
        return "/users"
    return f"/users?{parsed.query}" if parsed.query else "/users"


def _user_deletion_batch_url(batch_id: int, *, return_to: str = "/users", error: str = "") -> str:
    parsed = urlsplit(_safe_users_return_url(return_to))
    query: list[tuple[str, str | int]] = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"deletion_batch_id", "deletion_error"}
    ]
    query.append(("deletion_batch_id", batch_id))
    if error:
        query.append(("deletion_error", error))
    return f"/users?{urlencode(query)}#user-deletion"


def _users_return_url_with_deletion_error(return_to: str, error: str) -> str:
    parsed = urlsplit(_safe_users_return_url(return_to))
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "deletion_error"
    ]
    query.append(("deletion_error", error))
    return f"/users?{urlencode(query)}"


def _render_user_workbench(
    request: Request,
    db: Session,
    *,
    error: str = "",
    outcomes: list[dict[str, str]] | None = None,
    action_label: str = "",
    deletion_batch_id: int | None = None,
    deletion_error: str = "",
):
    return templates.TemplateResponse(
        request,
        "users.html",
        _user_workbench_context(
            request,
            db,
            error=error,
            outcomes=outcomes or [],
            action_label=action_label,
            deletion_batch_id=deletion_batch_id,
            deletion_error=deletion_error,
        ),
    )


def _user_workbench_context(
    request: Request,
    db: Session,
    *,
    query: str = "",
    site_id: int | None = None,
    site_ids: set[int] | None = None,
    site_scope: str = "selected",
    role: str = "all",
    customer_status: str = "all",
    error: str = "",
    outcomes: list[dict[str, str]] | None = None,
    action_label: str = "",
    fresh_users: str = "",
    active_refresh_run_id: int | None = None,
    message: str = "",
    deletion_batch_id: int | None = None,
    deletion_error: str = "",
) -> dict:
    service = SiteUserService(db=db, cipher=get_secret_cipher())
    entries = user_entries(_website_gateway(request, db))
    if role != "all" and role not in SiteUserService.ROLE_OPTIONS:
        role = "all"
    status_options = sorted(
        {
            entry.site.customer.zoho_status
            for entry in entries
            if entry.site.customer is not None and entry.site.customer.zoho_status
        }
    )
    if customer_status != "all" and customer_status not in status_options:
        customer_status = "all"
    selected_site_ids = site_ids if site_scope == "selected" else None
    if selected_site_ids is None and site_id is not None:
        selected_site_ids = {site_id}
    filtered_entries = service.filter_workbench_entries(
        entries,
        query=query,
        site_ids=selected_site_ids,
        role=role,
        customer_status=customer_status,
    )
    site_options = sorted(
        (
            site
            for site in website_sites(_website_gateway(request, db))
            if site.status == "verified"
            and any(capability.ability_name == SiteUserService.CREATE_ABILITY for capability in site.capabilities)
        ),
        key=lambda site: site.domain.casefold(),
    )
    outcome_rows = outcomes or []
    refresh_service = FleetRefreshService(db=db)
    active_refresh_run = refresh_service.get_active_run(modes=FleetRefreshService.user_modes())
    progress_refresh_run = (
        refresh_service.get_run(active_refresh_run_id)
        if active_refresh_run_id is not None
        else active_refresh_run
    )
    if progress_refresh_run is not None and progress_refresh_run.mode not in FleetRefreshService.user_modes():
        progress_refresh_run = active_refresh_run
    completion_url = ""
    if fresh_users == "running" and progress_refresh_run is not None and progress_refresh_run.mode == FleetRefreshService.MODE_FRESH_USERS:
        completion_query: list[tuple[str, str | int]] = [("site_scope", "selected")]
        completion_query.extend(("site_id", selected_id) for selected_id in sorted(selected_site_ids or []))
        completion_query.extend(
            [("q", query), ("role", role), ("customer_status", customer_status), ("fresh_users", "ok"), ("message", "Fresh user inventory checks completed.")]
        )
        completion_url = f"/users?{urlencode(completion_query)}"
    deletion_return_query: list[tuple[str, str | int]] = [("site_scope", site_scope)]
    deletion_return_query.extend(("site_id", selected_id) for selected_id in sorted(selected_site_ids or []))
    deletion_return_query.extend(
        [("q", query), ("role", role), ("customer_status", customer_status)]
    )
    deletion_return_url = f"/users?{urlencode(deletion_return_query)}"
    deletion_service = UserDeletionBatchService(db=db, cipher=get_secret_cipher())
    deletion_batch = deletion_service.get_batch(deletion_batch_id) if deletion_batch_id is not None else None
    deletion_rows = deletion_service.preparation_rows(deletion_batch) if deletion_batch is not None else []
    deletion_batch_can_start = deletion_batch is not None and deletion_service.batch_can_start(deletion_batch, deletion_rows)
    if deletion_batch is not None and deletion_batch.status in {
        UserDeletionBatchService.BATCH_QUEUED,
        UserDeletionBatchService.BATCH_RUNNING,
    }:
        schedule_pending_user_deletions()
    return {
        "entries": filtered_entries,
        "summary": {
            "users": len(entries),
            "sites": len({entry.site.id for entry in entries}),
            "administrators": sum("administrator" in entry.user["roles"] for entry in entries),
            "role_ready": sum(entry.supports_role_change for entry in entries),
        },
        "filters": {
            "q": query,
            "site_ids": sorted(selected_site_ids or []),
            "site_scope": site_scope,
            "role": role,
            "customer_status": customer_status,
        },
        "site_options": site_options,
        "site_selector": build_site_selector_context(
            action="/users",
            form_id="user-site-scope-form",
            sites=site_options,
            selected_site_ids=selected_site_ids,
            site_scope=site_scope,
            submit_label="Start",
            csrf_token=get_csrf_token(request),
            secondary_submit_action="/users/fresh-show",
            secondary_primary_label="Gespeicherte Benutzer anzeigen",
            secondary_submit_label="Benutzer frisch prüfen",
        ),
        "role_options": SiteUserService.ROLE_OPTIONS,
        "customer_status_options": status_options,
        "csrf_token": get_csrf_token(request),
        "error": error,
        "outcomes": outcome_rows,
        "action_label": action_label,
        "bulk_limit": SiteUserService.BULK_ACTION_LIMIT,
        "deletion_text_confirmation_threshold": UserDeletionBatchService.TEXT_CONFIRMATION_THRESHOLD,
        "fresh_users": fresh_users,
        "message": message,
        "deletion_error": deletion_error,
        "deletion_batch": deletion_batch,
        "deletion_rows": deletion_rows,
        "deletion_batch_can_start": deletion_batch_can_start,
        "deletion_return_url": deletion_return_url,
        "progress_refresh_run": progress_refresh_run,
        "refresh_completion_url": completion_url,
        "refresh_completion_label": "Benutzerprüfung abgeschlossen",
        "refresh_return_path": "/users",
        "refresh_eyebrow": "Aktuelles Benutzerinventar",
        "refresh_item_count": len(filtered_entries),
    }


def _backup_workbench_context(
    request: Request,
    db: Session,
    *,
    site_ids: set[int] | None,
    site_scope: str,
    fresh_backups: str,
    active_refresh_run_id: int | None,
    message: str,
) -> dict:
    repository = SiteRepository(db)
    site_options, selected_sites, snapshots = backup_workbench(_website_gateway(request, db), None if site_scope == "all" else (site_ids or set()))
    effective_site_ids = {site.id for site in site_options} if site_scope == "all" else (site_ids or set())
    refresh_service = FleetRefreshService(db=db)
    active_refresh_run = refresh_service.get_active_run(modes=FleetRefreshService.backup_modes())
    progress_refresh_run = (
        refresh_service.get_run(active_refresh_run_id)
        if active_refresh_run_id is not None
        else active_refresh_run
    )
    if progress_refresh_run is not None and progress_refresh_run.mode not in FleetRefreshService.backup_modes():
        progress_refresh_run = active_refresh_run
    completion_url = ""
    if fresh_backups == "running" and progress_refresh_run is not None and progress_refresh_run.mode == FleetRefreshService.MODE_FRESH_BACKUPS:
        completion_query: list[tuple[str, str | int]] = [("site_scope", "selected")]
        completion_query.extend(("site_id", selected_id) for selected_id in sorted(effective_site_ids))
        completion_query.extend([("fresh_backups", "ok"), ("message", "Fresh backup status checks completed.")])
        completion_url = f"/backups?{urlencode(completion_query)}"
    available = sum(bool(snapshot and snapshot.backup_available) for snapshot in snapshots.values())
    complete = sum(bool(snapshot and snapshot.backup_complete) for snapshot in snapshots.values())
    return {
        "entries": [{"site": site, "snapshot": snapshots.get(site.id)} for site in selected_sites],
        "summary": {"sites": len(selected_sites), "available": available, "complete": complete},
        "site_selector": build_site_selector_context(
            action="/backups",
            form_id="backup-site-scope-form",
            sites=site_options,
            selected_site_ids=effective_site_ids,
            site_scope=site_scope,
            submit_label="Start",
            csrf_token=get_csrf_token(request),
            secondary_submit_action="/backups/fresh-show",
            secondary_primary_label="Gespeicherte Backupstatus anzeigen",
            secondary_submit_label="Backupstatus frisch prüfen",
        ),
        "csrf_token": get_csrf_token(request),
        "fresh_backups": fresh_backups,
        "message": message,
        "progress_refresh_run": progress_refresh_run,
        "refresh_completion_url": completion_url,
        "refresh_completion_label": "Backupstatus-Prüfung abgeschlossen",
        "refresh_return_path": "/backups",
        "refresh_eyebrow": "Aktueller Backupstatus",
        "refresh_item_count": len(selected_sites),
    }


def _site_users_redirect(site_id: int, result: str, message: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/sites/{site_id}?{urlencode({'users': result, 'message': message})}#users",
        status_code=303,
    )








def _safe_updates_return_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path != "/updates":
        return "/updates"
    return f"/updates?{parsed.query}" if parsed.query else "/updates"


def _updates_return_url_with_message(return_url: str, message: str) -> str:
    parsed = urlsplit(return_url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "message"]
    query.append(("message", message))
    return f"/updates?{urlencode(query)}"


def _customer_communication_service(db: Session) -> CustomerCommunicationService:
    return CustomerCommunicationService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )


def _contact_create_context(
    request: Request,
    db: Session,
    *,
    selected_customer_id: int | None = None,
    submitted_values: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    directory = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    user = _require_hub_admin(request)
    allowed = HubAccessControlService(db=db).accessible_record_ids(user=user, module_key="customers")
    customers = tuple(entry.customer for entry in directory.list_entries(allowed_customer_ids=allowed))
    selected_customer = next((customer for customer in customers if customer.id == selected_customer_id), None)
    return {
        "customers": customers,
        "fields": _ordered_layout_fields(
            db,
            layout_key=CONTACT_FIELDS_LAYOUT_KEY,
            fields=contact_fields(creating=True),
        ),
        "selected_customer_id": selected_customer_id,
        "selected_customer": selected_customer,
        "submitted_values": submitted_values or {},
        "error": error,
        "csrf_token": get_csrf_token(request),
    }


def _case_create_context(
    request: Request,
    db: Session,
    *,
    selected_customer_id: int | None = None,
    submitted_values: dict[str, str] | None = None,
    source_email: HubCaseEmailSource | None = None,
    error: str | None = None,
) -> dict[str, object]:
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    values = service.new_form_values(source_email=source_email)
    values.update(submitted_values or {})
    user = getattr(request.state, "hub_user", None)
    access = HubAccessControlService(db=db)
    customers = tuple(customer for customer in service.list_linkable_customers()
        if user is not None and access.can_access_record(user=user, module_key="customers", record_id=customer.id))
    selected_customer = next((customer for customer in customers if customer.id == selected_customer_id), None)
    return {
        "fields": _ordered_layout_fields(
            db,
            layout_key=CASE_FIELDS_LAYOUT_KEY,
            fields=HUB_CASE_FIELDS,
        ),
        "customers": customers,
        "selected_customer_id": selected_customer_id,
        "selected_customer": selected_customer,
        "source_email": source_email,
        "submitted_values": values,
        "error": error,
        "csrf_token": get_csrf_token(request),
    }


def _finance_gateway(request: Request, db: Session) -> HubOperationService:
    user = _require_hub_admin(request)
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username)


def _finance_read(request: Request, db: Session, kind: str, record_id: int):
    try:
        return finance_detail(_finance_gateway(request, db), kind, record_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _finance_article_create_context(
    request: Request,
    db: Session,
    *,
    submitted_values: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    values = HubFinanceService(db=db, cipher=get_secret_cipher()).new_article_values()
    values.update(submitted_values or {})
    return {
        "fields": _ordered_layout_fields(
            db,
            layout_key=ARTICLE_FIELDS_LAYOUT_KEY,
            fields=ARTICLE_FIELDS,
        ),
        "submitted_values": values,
        "error": error,
        "csrf_token": get_csrf_token(request),
    }


def _finance_article_detail_context(
    request: Request,
    *,
    detail: object,
    fields: str,
    fields_message: str,
    layout: str,
    layout_message: str,
) -> dict[str, object]:
    return {
        "detail": detail,
        "fields_state": fields if fields in {"success", "error"} else "",
        "fields_message": fields_message[:500] if fields in {"success", "error"} else "",
        "layout_state": layout if layout in {"success", "error"} else "",
        "layout_message": layout_message[:500] if layout in {"success", "error"} else "",
        "csrf_token": get_csrf_token(request),
    }


def _finance_offer_create_context(
    request: Request,
    db: Session,
    *,
    selected_customer_id: int | None = None,
    selected_lead_id: int | None = None,
    selected_contact_id: int | None = None,
    selected_pdf_template_id: int | None = None,
    submitted_values: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    service = HubFinanceService(db=db, cipher=get_secret_cipher())
    values = service.new_offer_values(offer_date=(submitted_values or {}).get("offer_field__offer_date"))
    values["offer_field__notes"] = service.offer_notes({})
    values.update(submitted_values or {})
    pdf_templates = HubPdfTemplateService(db=db).list_templates(document_type="offers")
    options = finance_options(_finance_gateway(request, db), "offers")
    customers = options["customers"]
    leads = options["leads"]
    selected_customer = next((customer for customer in customers if customer.id == selected_customer_id), None)
    selected_lead = next((lead for lead in leads if lead.id == selected_lead_id), None)
    return {
        "fields": _ordered_layout_fields(
            db,
            layout_key=OFFER_FIELDS_LAYOUT_KEY,
            fields=tuple(field for field in OFFER_FIELDS if field.section == "fields"),
        ),
        "articles": service.article_options(),
        "customers": customers,
        "leads": leads,
        "contacts": options["contacts"],
        "selected_customer_id": selected_customer_id,
        "selected_customer": selected_customer,
        "selected_lead_id": selected_lead_id,
        "selected_lead": selected_lead,
        "selected_contact_id": selected_contact_id,
        "pdf_templates": pdf_templates,
        "selected_pdf_template_id": selected_pdf_template_id or next((item.id for item in pdf_templates if item.is_default), None),
        "submitted_values": values,
        "line_rows": _finance_offer_line_form_rows(values),
        "error": error,
        "csrf_token": get_csrf_token(request),
        **_finance_position_preset_context(db, SALES_POSITION_PRESET_LIBRARY),
    }


def _finance_offer_detail_context(
    request: Request,
    *,
    service: HubFinanceService,
    detail: object,
    fields: str,
    fields_message: str,
    layout: str,
    layout_message: str,
) -> dict[str, object]:
    line_rows = []
    for line in detail.lines:
        line_rows.append({
            "article_id": str(line.article_id or ""),
            "name": line.name,
            "sku": line.sku,
            "description": line.description,
            "quantity": line.quantity,
            "unit": line.unit,
            "unit_price": line.unit_price,
            "discount_percent": line.discount_percent,
            "tax_rate": line.tax_rate,
        })
    if not line_rows:
        line_rows = _finance_offer_line_form_rows(service.new_offer_values())
    options = finance_options(_finance_gateway(request, service.db), "offers")
    pdf_templates = HubPdfTemplateService(db=service.db).list_templates(document_type="offers")
    return {
        "detail": detail,
        "articles": service.article_options(),
        "customers": options["customers"],
        "leads": options["leads"],
        "contacts": options["contacts"],
        "line_rows": line_rows,
        "pdf_templates": pdf_templates,
        "selected_pdf_template_id": detail.offer.pdf_template_id or next((item.id for item in pdf_templates if item.is_default), None),
        "generated_pdf": HubFinancePdfService(db=service.db, cipher=get_secret_cipher()).view(
            document_type="offers", document_id=detail.offer.id
        ),
        "fields_state": fields if fields in {"success", "error"} else "",
        "fields_message": fields_message[:500] if fields in {"success", "error"} else "",
        "layout_state": layout if layout in {"success", "error"} else "",
        "layout_message": layout_message[:500] if layout in {"success", "error"} else "",
        "csrf_token": get_csrf_token(request),
        **_finance_position_preset_context(service.db, SALES_POSITION_PRESET_LIBRARY),
    }


def _finance_offer_line_form_rows(values: dict[str, str]) -> list[dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for key, value in values.items():
        parts = key.split("__")
        if len(parts) != 3 or parts[0] != "offer_line" or not parts[1].isdigit():
            continue
        rows.setdefault(int(parts[1]), {})[parts[2]] = str(value)
    defaults = {
        "article_id": "",
        "name": "",
        "sku": "",
        "description": "",
        "quantity": "1",
        "unit": "",
        "unit_price": "",
        "discount_percent": "0",
        "tax_rate": "19",
    }
    return [{**defaults, **rows[index]} for index in sorted(rows)] or [defaults]


def _finance_document_module(module_key: str):
    module = FINANCE_DOCUMENT_MODULES.get(module_key)
    if module is None:
        raise HTTPException(status_code=404, detail="Finance module not found.")
    return module


def _finance_position_preset_library(module: object) -> str | None:
    if module.key == "orders":
        return SALES_POSITION_PRESET_LIBRARY
    if module.key == "invoices":
        return INVOICE_POSITION_PRESET_LIBRARY
    return None


def _finance_position_preset_json(preset: FinancePositionPresetView) -> dict[str, object]:
    return {
        "id": preset.id,
        "name": preset.name,
        "line_count": preset.line_count,
        "lines": [dict(line) for line in preset.lines],
    }


def _finance_position_preset_context(db: Session, library_key: str | None) -> dict[str, object]:
    if library_key is None:
        return {
            "finance_position_preset_library_key": None,
            "finance_position_preset_library_label": "",
            "finance_position_presets": (),
            "finance_position_presets_json": [],
        }
    presets = HubFinancePositionPresetService(
        db=db,
        cipher=get_secret_cipher(),
    ).list_presets(library_key=library_key)
    return {
        "finance_position_preset_library_key": library_key,
        "finance_position_preset_library_label": POSITION_PRESET_LIBRARIES[library_key],
        "finance_position_presets": presets,
        "finance_position_presets_json": [
            _finance_position_preset_json(preset) for preset in presets
        ],
    }


def _finance_document_create_context(
    request: Request,
    db: Session,
    *,
    module: object,
    selected_customer_id: int | None = None,
    selected_contact_id: int | None = None,
    selected_link_id: int | None = None,
    selected_pdf_template_id: int | None = None,
    source_invoice_id: int | None = None,
    submitted_values: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    service = HubFinanceDocumentService(db=db, cipher=get_secret_cipher())
    values = service.new_form_values(module=module)
    source_invoice = None
    if module is DUNNING_MODULE and source_invoice_id is not None:
        _finance_read(request, db, "invoices", source_invoice_id)
        source_invoice = service.dunning_draft_from_invoice(invoice_id=source_invoice_id)
        selected_customer_id = source_invoice.customer_id
        selected_contact_id = source_invoice.contact_id
        selected_link_id = source_invoice.invoice_id
        values.update(source_invoice.submitted_values)
    values.update(submitted_values or {})
    if module.is_recurring:
        count = values.get("document_field__custom_interval_count", "")
        unit = values.get("document_field__custom_interval_unit", "")
        values["document_field__custom_interval"] = f"{count}:{unit}" if count or unit else ""
        due_count = values.get("document_field__payment_due_count", "")
        due_unit = values.get("document_field__payment_due_unit", "")
        values["document_field__payment_due"] = f"{due_count}:{due_unit}" if due_count or due_unit else ""
    template_type = "invoices" if module.is_recurring else module.key
    pdf_templates = HubPdfTemplateService(db=db).list_templates(document_type=template_type) if template_type in {"orders", "invoices", "dunnings"} else ()
    options = finance_options(_finance_gateway(request, db), module.key)
    customers = options["customers"]
    selected_customer = next((customer for customer in customers if customer.id == selected_customer_id), None)
    return {
        "module": module,
        "fields": _ordered_layout_fields(
            db,
            layout_key=module.layout_key,
            fields=module.fields,
        ),
        "articles": service.article_options(),
        "customers": customers,
        "contacts": options["contacts"],
        "link_options": options["link_options"],
        "selected_customer_id": selected_customer_id,
        "selected_customer": selected_customer,
        "selected_contact_id": selected_contact_id,
        "selected_link_id": selected_link_id,
        "source_invoice": source_invoice,
        "pdf_templates": pdf_templates,
        "selected_pdf_template_id": selected_pdf_template_id or next((item.id for item in pdf_templates if item.is_default), None),
        "submitted_values": values,
        "line_rows": _finance_document_line_form_rows(values),
        "error": error,
        "csrf_token": get_csrf_token(request),
        **_finance_position_preset_context(
            db,
            _finance_position_preset_library(module),
        ),
    }


def _finance_document_detail_context(
    request: Request,
    *,
    service: HubFinanceDocumentService,
    module: object,
    detail: object,
    fields: str,
    fields_message: str,
    layout: str,
    layout_message: str,
    email: str,
    email_message: str,
) -> dict[str, object]:
    line_rows = [
        {
            "article_id": str(line.article_id or ""),
            "name": line.name,
            "sku": line.sku,
            "description": line.description,
            "quantity": line.quantity,
            "unit": line.unit,
            "unit_price": line.unit_price,
            "discount_percent": line.discount_percent,
            "tax_rate": line.tax_rate,
        }
        for line in detail.lines
    ] or _finance_document_line_form_rows(service.new_form_values(module=module))
    selected_link_id = getattr(detail.document, f"{module.link_attribute}_id", None) if module.link_attribute else None
    template_type = "invoices" if module.is_recurring else module.key
    pdf_templates = HubPdfTemplateService(db=service.db).list_templates(document_type=template_type) if template_type in {"orders", "invoices", "dunnings"} else ()
    options = finance_options(_finance_gateway(request, service.db), module.key)
    current_user = getattr(request.state, "hub_user", None)
    access = HubAccessControlService(db=service.db)
    can_view_dunning_emails = bool(
        module.is_dunning
        and current_user is not None
        and access.can(current_user, "emails", "view")
    )
    return {
        "module": module,
        "detail": detail,
        "articles": service.article_options(),
        "customers": options["customers"],
        "contacts": options["contacts"],
        "link_options": options["link_options"],
        "selected_link_id": selected_link_id,
        "line_rows": line_rows,
        "pdf_templates": pdf_templates,
        "selected_pdf_template_id": getattr(detail.document, "pdf_template_id", None) or next((item.id for item in pdf_templates if item.is_default), None),
        "generated_pdf": HubFinancePdfService(db=service.db, cipher=get_secret_cipher()).view(
            document_type=module.key, document_id=detail.document.id
        ) if module.key in {"orders", "invoices", "dunnings"} else None,
        "dunning_emails": (
            CustomerCommunicationService(
                db=service.db,
                cipher=get_secret_cipher(),
                public_base_url=get_settings().public_base_url,
            ).get_dunning_email_views(dunning_id=detail.document.id)
            if can_view_dunning_emails
            else ()
        ),
        "can_view_dunning_emails": can_view_dunning_emails,
        "can_create_dunning_email": bool(
            can_view_dunning_emails
            and detail.document.customer_id is not None
            and access.can(current_user, "emails", "create")
        ),
        "email_state": email if email in {"success", "warning", "error"} else "",
        "email_message": email_message[:500] if email in {"success", "warning", "error"} else "",
        "fields_state": fields if fields in {"success", "error"} else "",
        "fields_message": fields_message[:500] if fields in {"success", "error"} else "",
        "layout_state": layout if layout in {"success", "error"} else "",
        "layout_message": layout_message[:500] if layout in {"success", "error"} else "",
        "csrf_token": get_csrf_token(request),
        **_finance_position_preset_context(
            service.db,
            _finance_position_preset_library(module),
        ),
    }


def _finance_document_line_form_rows(values: dict[str, str]) -> list[dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for key, value in values.items():
        parts = key.split("__")
        if len(parts) != 3 or parts[0] != "document_line" or not parts[1].isdigit():
            continue
        rows.setdefault(int(parts[1]), {})[parts[2]] = str(value)
    defaults = {
        "article_id": "",
        "name": "",
        "sku": "",
        "description": "",
        "quantity": "1",
        "unit": "",
        "unit_price": "",
        "discount_percent": "0",
        "tax_rate": "19",
    }
    return [{**defaults, **rows[index]} for index in sorted(rows)] or [defaults]


def _finance_submitted_values(form: object, *, prefix: str) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in form.items()
        if isinstance(value, str) and str(key).startswith(prefix)
    }


def _optional_form_id(raw_value: object) -> int | None:
    value = str(raw_value or "").strip()
    return int(value) if value else None


def _lead_create_context(
    request: Request,
    db: Session,
    *,
    submitted_values: dict[str, object] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    values = HubLeadService(db=db, cipher=get_secret_cipher()).new_form_values()
    values.update(submitted_values or {})
    return {
        "fields": _ordered_layout_fields(
            db,
            layout_key=LEAD_FIELDS_LAYOUT_KEY,
            fields=HUB_LEAD_FIELDS,
        ),
        "subforms": HUB_LEAD_SUBFORMS,
        "submitted_values": values,
        "error": error,
        "csrf_token": get_csrf_token(request),
    }


def _lead_submitted_values(form: object) -> dict[str, object]:
    """Preserve multi-select values from the browser's repeated form keys."""
    values: dict[str, object] = {}
    multi_select_names = {
        f"lead_field__{field.key}"
        for field in HUB_LEAD_FIELDS
        if field.display_type == "Mehrfachauswahl"
    }
    for key, value in form.multi_items():
        if key == "csrf_token":
            continue
        if key in multi_select_names:
            values.setdefault(key, []).append(value)
        else:
            values[key] = value
    for field in HUB_LEAD_FIELDS:
        if not field.read_only and field.display_type in {"Mehrfachauswahl", "Boolesch"}:
            values.setdefault(f"lead_field__{field.key}", [] if field.display_type == "Mehrfachauswahl" else "false")
    return values


def _mailbox_linked_case(db: Session, message: object):
    """Resolve the optional case badge for a rendered mailbox message."""
    if message is None or getattr(message, "kind", "") in {"draft", "system", "scheduled"}:
        return None
    source_email_key = str(getattr(message, "key", "") or "")
    if not source_email_key:
        return None
    try:
        return HubCaseService(db=db, cipher=get_secret_cipher()).linked_case_for_source_email(
            source_email_key=source_email_key
        )
    except HubCaseError:
        return None


def _mailbox_case_payload(linked_case: object) -> dict[str, object]:
    """Build the small status payload used to refresh a mailbox case control."""
    case = getattr(linked_case, "case")
    status = str(getattr(linked_case, "status") or "")
    is_closed = status.casefold() == "abgeschlossen"
    return {
        "case_id": case.id,
        "case_number": getattr(linked_case, "case_number"),
        "case_url": f"/cases/{case.id}",
        "status": status,
        "label": "Fall abgeschlossen" if is_closed else "Offener Fall",
        "is_closed": is_closed,
    }


def _case_detail_context(
    request: Request,
    *,
    service: HubCaseService,
    detail: object,
    fields: str,
    fields_message: str,
    layout: str,
    layout_message: str,
    email_link: str,
    email_link_message: str,
    completion_email_template_id: str = "",
    completion_email_customer_id: int | None = None,
) -> dict[str, object]:
    user = getattr(request.state, "hub_user", None)
    access = HubAccessControlService(db=service.db)
    customers = tuple(customer for customer in service.list_linkable_customers()
        if user is not None and access.can_access_record(user=user, module_key="customers", record_id=customer.id))
    return {
        "detail": detail,
        "customers": customers,
        "fields_state": fields if fields in {"success", "error"} else "",
        "fields_message": fields_message[:500] if fields in {"success", "error"} else "",
        "layout_state": layout if layout in {"success", "error"} else "",
        "layout_message": layout_message[:500] if layout in {"success", "error"} else "",
        "email_link_state": email_link if email_link in {"success", "error"} else "",
        "email_link_message": email_link_message[:500] if email_link in {"success", "error"} else "",
        "completion_email_template_id": completion_email_template_id,
        "completion_email_customer_id": completion_email_customer_id,
        "csrf_token": get_csrf_token(request),
    }


def _execute_contact_operation(db: Session, actor: str, action: str, **values):
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=actor).execute(
        f"contacts.{action}", {key: str(value) if value is not None else "" for key, value in values.items()},
    )


def _contact_detail_context(
    request: Request,
    db: Session,
    *,
    detail: object,
    fields: str,
    fields_message: str,
    layout: str,
    layout_message: str,
) -> dict[str, object]:
    directory = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    user = getattr(request.state, "hub_user", None)
    access = HubAccessControlService(db=db)
    if not access.can_access_contact(user=user, contact=detail.contact):
        raise HTTPException(status_code=404, detail="Contact not found.")
    allowed = access.accessible_record_ids(user=user, module_key="customers")
    return {
        "detail": detail,
        "can_manage_contacts": access.can(user, "contacts", "edit"),
        "can_delete_contacts": access.can(user, "contacts", "delete"),
        "can_sync_contacts": access.can(user, "contacts", "manage"),
        "linkable_customers": tuple(entry.customer for entry in directory.list_entries(allowed_customer_ids=allowed)),
        "fields_state": fields if fields in {"success", "error"} else "",
        "fields_message": fields_message[:500] if fields in {"success", "error"} else "",
        "layout_state": layout if layout in {"success", "error"} else "",
        "layout_message": layout_message[:500] if layout in {"success", "error"} else "",
        "csrf_token": get_csrf_token(request),
    }


def _email_compose_response(request: Request, redirect: RedirectResponse, *, error: str | None = None) -> Response:
    location = urlsplit(redirect.headers["location"])
    account_id = request.query_params.get("account_id", "")
    if location.path == "/emails" and account_id.isascii() and account_id.isdecimal() and 0 < len(account_id) <= 10:
        query = dict(parse_qsl(location.query))
        query["account_id"] = account_id
        redirect.headers["location"] = location._replace(query=urlencode(query)).geturl()
    if "application/json" in request.headers.get("accept", ""):
        if error is not None:
            return JSONResponse({"detail": error}, status_code=400)
        return JSONResponse({"redirect_url": redirect.headers["location"]})
    return redirect


def _customer_communication_redirect(customer_id: int, state: str, message: str) -> RedirectResponse:
    query = urlencode({"communication": state, "message": message[:500]})
    return RedirectResponse(url=f"/customers/{customer_id}?{query}#customer-communications", status_code=303)


def _dunning_email_redirect(dunning_id: int, state: str, message: str) -> RedirectResponse:
    query = urlencode({"email": state, "email_message": message[:500]})
    return RedirectResponse(url=f"/finance/dunnings/{dunning_id}?{query}#dunning-emails", status_code=303)


def _customer_activity_redirect(customer_id: int, state: str, message: str) -> RedirectResponse:
    query = urlencode({"activity": state, "activity_message": message[:500]})
    return RedirectResponse(url=f"/customers/{customer_id}?{query}#customer-activities", status_code=303)


def _calendar_week_start(value: str, *, today: date) -> date:
    try:
        selected = date.fromisoformat(value) if value else today
    except ValueError:
        selected = today
    return selected - timedelta(days=selected.weekday())


def _calendar_week_label(week_start: date, week_end: date) -> str:
    start_month = _GERMAN_MONTH_NAMES[week_start.month - 1]
    end_month = _GERMAN_MONTH_NAMES[week_end.month - 1]
    if week_start.year == week_end.year and week_start.month == week_end.month:
        return f"{week_start.day}. – {week_end.day}. {start_month} {week_start.year}"
    if week_start.year == week_end.year:
        return f"{week_start.day}. {start_month} – {week_end.day}. {end_month} {week_start.year}"
    return f"{week_start.day}. {start_month} {week_start.year} – {week_end.day}. {end_month} {week_end.year}"


def _next_task_due_date(berlin_now: datetime) -> date:
    return berlin_now.date() + timedelta(days=1)


def _calendar_redirect(week_start: date, state: str, message: str) -> RedirectResponse:
    query = urlencode({"week": week_start.isoformat(), "calendar": state, "calendar_message": message[:500]})
    return RedirectResponse(url=f"/calendar?{query}", status_code=303)


def _mailbox_url(*, folder: str, unread: bool, selected: str) -> str:
    query = {"folder": folder, "selected": selected}
    if unread:
        query["unread"] = "true"
    return f"/emails?{urlencode(query)}"


def _styling_context(request: Request, service: StylingSettingsService, error: str | None = None) -> dict:
    return {
        "styling": service.get_runtime_settings(),
        "font_family_options": [
            {"key": key, "label": label}
            for key, label, _ in FONT_FAMILY_OPTIONS
        ],
        "error": error,
        "csrf_token": get_csrf_token(request),
    }


def _require_hub_admin(request: Request):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if user.role != "admin":
        target = permission_target(request.url.path, request.method)
        if target is None:
            raise HTTPException(status_code=403, detail="Access denied.")
        with SessionLocal() as db:
            if not HubAccessControlService(db=db).can(user, target[0], target[1]):
                raise HTTPException(status_code=403, detail="Access denied.")
    return user
