from datetime import date, datetime, time, timedelta
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
from app.core.templates import _unread_email_count_for_db, create_templates
from app.db.session import get_db
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
from app.services.customer_directory import CONTACT_FIELDS_LAYOUT_KEY, CUSTOMER_FIELDS_LAYOUT_KEY, CustomerDirectoryService
from app.services.customer_communications import (
    CustomerCommunicationAttachmentUpload,
    CustomerCommunicationImageError,
    CustomerCommunicationService,
)
from app.services.email_compose_images import EmailComposeImageError, EmailComposeImageService
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
from app.services.hub_mailbox import HubMailboxService, MAILBOX_FOLDERS
from app.services.hub_mailbox_transport import DEFAULT_HUB_MAILBOX_SENDER_EMAIL
from app.services.task_email_reminder_worker import TaskEmailReminderWorker
from app.models.customer import Customer
from app.services.site_selection import SELECTABLE_CUSTOMER_STATUSES, build_site_selector_context
from app.services.styling_settings import FONT_FAMILY_OPTIONS, StylingSettingsError, StylingSettingsService
from app.services.module_layouts import ModuleLayoutError, ModuleLayoutService
from app.services.plugin_installation_packages import PluginInstallationPackageService, PluginPackageError
from app.services.zoho_crm import ZOHO_RELEVANT_ACCOUNT_STATUSES, ZohoCrmError, ZohoCrmService
from app.services.zoho_contact_field_catalog import ZOHO_CONTACT_FIELDS
from app.services.hub_case_field_catalog import HUB_CASE_FIELDS
from app.services.hub_cases import CASE_FIELDS_LAYOUT_KEY, HubCaseEmailSource, HubCaseError, HubCaseService
from app.services.hub_workflows import CASE_COMPLETION_EMAIL_TEMPLATE_ID
from app.services.zoho_case_import import ZohoCaseImportService

templates = create_templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(include_in_schema=False)

_GERMAN_WEEKDAY_NAMES = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_GERMAN_MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Annotated[Session, Depends(get_db)]):
    repository = SiteRepository(db)
    summary = DashboardSummary.model_validate(repository.get_dashboard_summary())
    latest_sites = repository.list_sites(limit=10)
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    inventory_summary = inventory_service.summarize(inventory_service.list_items(limit=1000))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "summary": summary,
            "inventory_summary": inventory_summary,
            "sites": latest_sites,
        },
    )


@router.get("/styling", response_class=HTMLResponse)
def styling_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_hub_admin(request)
    return templates.TemplateResponse(
        request,
        "styling.html",
        _styling_context(request, StylingSettingsService(db=db)),
    )


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
        service.configure(
            actor=user,
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
    except StylingSettingsError as exc:
        return templates.TemplateResponse(
            request,
            "styling.html",
            _styling_context(request, service, error=str(exc)),
            status_code=400,
        )
    write_audit_log(db, site=None, actor=user.username, source="hub-styling", action="update-global-styling", result="success")
    db.commit()
    return RedirectResponse(url="/styling?styling=saved", status_code=303)


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
    all_items = inventory_service.list_items(limit=1000)
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
    service = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    can_manage_customer_fields = getattr(request.state, "hub_user", None) is not None and request.state.hub_user.role == "admin"
    industry_options = service.list_industries()
    entries = service.list_entries(
        query=q,
        status=None if status == "all" else status,
        industry=None if industry == "all" else industry,
        unread_email_only=email == "unread",
        include_sensitive=can_manage_customer_fields,
    )
    candidate_count = sum(entry.exact_match_candidate is not None for entry in entries)
    return templates.TemplateResponse(
        request,
        "customers.html",
        {
            "entries": entries,
            "candidate_count": candidate_count,
            "filters": {"q": q, "status": status, "industry": industry, "email": email},
            "status_options": ZOHO_RELEVANT_ACCOUNT_STATUSES,
            "industry_options": industry_options,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/customers/suggestions", response_class=JSONResponse)
def customer_suggestions(
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

    entries = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).list_entries(
        query=query,
        status=None if status == "all" else status,
        industry=None if industry == "all" else industry,
        unread_email_only=email == "unread",
    )
    return {
        "suggestions": [
            {
                "id": entry.customer.id,
                "name": entry.customer.name,
                "website": entry.customer.website_domain or "",
                "status": entry.account_status or "",
            }
            for entry in entries[:7]
        ]
    }


@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    created: int | None = None,
    deleted: bool = False,
    sync: str = "",
    sync_message: str = "",
):
    _require_hub_admin(request)
    service = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    return templates.TemplateResponse(
        request,
        "contacts.html",
        {
            "entries": service.list_contact_entries(),
            "created": created,
            "deleted": deleted,
            "sync_state": sync if sync in {"success", "error"} else "",
            "sync_message": sync_message[:500] if sync in {"success", "error"} else "",
            "csrf_token": get_csrf_token(request),
        },
    )


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
            "entries": HubCaseService(db=db, cipher=get_secret_cipher()).list_cases(),
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
            selected_customer_id=source_email.customer_id if source_email is not None else None,
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
        customer_id = int(raw_customer_id) if raw_customer_id else None
        source_email = service.source_email(source_email_key=source_email_key) if source_email_key else None
        if source_email is not None and source_email.customer_id is not None and customer_id != source_email.customer_id:
            raise HubCaseError("Der Kundenbezug der ausgewählten E-Mail darf beim Anlegen nicht geändert werden.")
        case = service.create_case(
            customer_id=customer_id,
            submitted_values=submitted_values,
            actor_username=user.username,
        )
        if source_email is not None:
            service.link_email(case_id=case.id, source_email_key=source_email.key)
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
            f"Created Hub Case {case.id} and linked one selected email; case data is not retained in the audit log."
            if source_email is not None
            else f"Created Hub Case {case.id}; case data is not retained in the audit log."
        ),
    )
    db.commit()
    if source_email is not None:
        query = urlencode({"email_link": "success", "email_link_message": "Die ausgewählte E-Mail wurde mit diesem Fall verknüpft."})
        return RedirectResponse(url=f"/cases/{case.id}?{query}#case-emails", status_code=303)
    return RedirectResponse(url=f"/cases/{case.id}", status_code=303)


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
        service.link_email(case_id=case_id, source_email_key=source_email.key)
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
    detail = service.get_detail(case_id=case_id)
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
        HubCaseService(db=db, cipher=get_secret_cipher()).unlink_email(case_id=case_id, link_id=link_id)
    except HubCaseError as exc:
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
        customer_id = int(raw_customer_id) if raw_customer_id else None
        service = HubCaseService(db=db, cipher=get_secret_cipher())
        existing_detail = service.get_detail(case_id=case_id)
        if existing_detail is None:
            raise HubCaseError("Der Fall wurde nicht gefunden.")
        was_completed = HubCaseService.is_completed_status(existing_detail.status)
        case = service.update_case(
            case_id=case_id,
            customer_id=customer_id,
            submitted_values=submitted_values,
        )
        was_completed_now = (
            not was_completed
            and HubCaseService.is_completed_status(submitted_values.get("case_field__status", ""))
        )
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
        detail=f"Updated Hub Case {case.id}; case data is not retained in the audit log.",
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
        ModuleLayoutService(db=db).configure(
            actor=user,
            layout_key=CASE_FIELDS_LAYOUT_KEY,
            item_order_json=str(form.get("order_json") or ""),
            allowed_keys=tuple(field.key for field in detail.fields),
        )
    except ModuleLayoutError as exc:
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
        case = HubCaseService(db=db, cipher=get_secret_cipher()).delete_case(case_id=case_id)
    except HubCaseError as exc:
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
        detail=f"Deleted Hub Case {case.id}; no Zoho record was changed.",
    )
    db.commit()
    return RedirectResponse(url="/cases?deleted=true", status_code=303)


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    week: str = "",
    calendar: str = "",
    calendar_message: str = "",
):
    berlin_now = datetime.now(ZoneInfo("Europe/Berlin"))
    week_start = _calendar_week_start(week, today=berlin_now.date())
    week_end = week_start + timedelta(days=6)
    activities = CustomerActivityService(db=db).list_calendar_activities(week_start=week_start)
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
    task_default_date = _next_task_due_date(berlin_now)
    if not week_start <= default_start.date() <= week_end:
        default_start = datetime.combine(week_start, time(hour=9))
    can_manage_calendar = getattr(request.state, "hub_user", None) is not None and request.state.hub_user.role == "admin"
    customers = tuple(
        db.scalars(
            select(Customer)
            .where(Customer.is_visible.is_(True))
            .order_by(Customer.name.asc(), Customer.id.asc())
        ).all()
    ) if can_manage_calendar else ()
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "calendar_days": calendar_days,
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
            "calendar_default_date": default_start.strftime("%Y-%m-%d"),
            "calendar_default_time": default_start.strftime("%H:%M"),
            "call_status_options": CALL_STATUS_OPTIONS,
            "call_duration_options": CALL_DURATION_OPTIONS,
            "call_time_options": CALL_TIME_OPTIONS,
            "call_reminder_channel_options": CALL_REMINDER_CHANNEL_OPTIONS,
            "call_reminder_options": CALL_REMINDER_OPTIONS,
            "call_defaults": {
                "start_date": default_start.strftime("%Y-%m-%d"),
                "start_time": default_start.strftime("%H:%M"),
                "duration_minutes": 30,
                "reminder_channel": "popup",
                "reminder_minutes_before": 5,
            },
            "task_defaults": {
                "due_date": task_default_date.isoformat(),
                "due_time": "09:00",
                "reminder_channel": "email",
                "reminder_minutes_before": 0,
            },
            "meeting_defaults": {
                "start_date": default_start.strftime("%Y-%m-%d"),
                "start_time": default_start.strftime("%H:%M"),
                "end_date": (default_start + timedelta(minutes=60)).strftime("%Y-%m-%d"),
                "end_time": (default_start + timedelta(minutes=60)).strftime("%H:%M"),
                "duration_minutes": 60,
                "reminder_channel": "popup",
                "reminder_minutes_before": 15,
            },
            "can_manage_calendar": can_manage_calendar,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.post("/calendar/activities")
def schedule_calendar_activity(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    customer_id: Annotated[str, Form()] = "",
    activity_kind: Annotated[str, Form()] = "meeting",
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
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    week_start = _calendar_week_start(week, today=datetime.now(ZoneInfo("Europe/Berlin")).date())
    service = CustomerActivityService(db=db)
    try:
        selected_customer_id = int(customer_id) if customer_id.strip() else None
    except ValueError:
        return _calendar_redirect(week_start, "error", "Bitte einen gültigen Kunden auswählen.")
    try:
        if activity_kind == "call":
            activity = service.schedule_call(
                customer_id=selected_customer_id,
                actor=user.username,
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
        elif activity_kind == "meeting":
            activity = service.schedule_meeting(
                customer_id=selected_customer_id,
                actor=user.username,
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
    except CustomerActivityError as exc:
        db.rollback()
        return _calendar_redirect(week_start, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action=action,
        result="ok",
        detail=f"Scheduled calendar {activity_kind} {activity.id} for customer {selected_customer_id or 'none'}; description is not retained in the audit log.",
    )
    db.commit()
    return _calendar_redirect(week_start, "success", message)


@router.post("/calendar/activities/{activity_kind}/{activity_id}")
def update_calendar_activity(
    activity_kind: str,
    activity_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    customer_id: Annotated[str, Form()] = "",
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
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    week_start = _calendar_week_start(week, today=datetime.now(ZoneInfo("Europe/Berlin")).date())
    try:
        selected_customer_id = int(customer_id) if customer_id.strip() else None
    except ValueError:
        return _calendar_redirect(week_start, "error", "Bitte einen gültigen Kunden auswählen.")

    service = CustomerActivityService(db=db)
    try:
        if activity_kind == "call":
            activity = service.update_calendar_call(
                call_id=activity_id,
                customer_id=selected_customer_id,
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
            activity = service.update_calendar_meeting(
                meeting_id=activity_id,
                customer_id=selected_customer_id,
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
    except CustomerActivityError as exc:
        db.rollback()
        return _calendar_redirect(week_start, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action=action,
        result="ok",
        detail=f"Updated calendar {activity_kind} {activity.id} for customer {selected_customer_id or 'none'}; description is not retained in the audit log.",
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
        contact = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).create_hub_contact(
            customer_id=customer_id,
            submitted_values=submitted_values,
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
        detail=f"Created Hub Contact {contact.id}; contact data is not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(url=f"/contacts/{contact.id}", status_code=303)


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
    detail = service.get_contact_detail_by_id(contact_id=contact_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Contact not found.")
    can_manage_contacts = getattr(request.state, "hub_user", None) is not None and request.state.hub_user.role == "admin"
    return templates.TemplateResponse(
        request,
        "customer_contact_detail.html",
        _contact_detail_context(
            request,
            db,
            detail=detail,
            can_manage_contacts=can_manage_contacts,
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
        contact = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).update_hub_contact(
            contact_id=contact_id,
            submitted_values=submitted_values,
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
        detail=f"Updated Hub Contact {contact.id}; contact data is not retained in the audit log.",
    )
    db.commit()
    query = urlencode({"fields": "success", "fields_message": "Kontaktdaten wurden im Hub gespeichert."})
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
        customer_id = int(raw_customer_id) if raw_customer_id else None
    except ValueError:
        customer_id = None
    try:
        contact = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).set_hub_contact_customer(
            contact_id=contact_id,
            customer_id=customer_id,
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
        detail=f"Updated the Hub customer link for Contact {contact.id}.",
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
        ModuleLayoutService(db=db).configure(
            actor=user,
            layout_key=CONTACT_FIELDS_LAYOUT_KEY,
            item_order_json=str(form.get("order_json") or ""),
            allowed_keys=tuple(field.key for field in detail.display_profile_fields),
        )
    except ModuleLayoutError as exc:
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
        contact = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).delete_contact_from_hub(contact_id=contact_id)
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
        detail=f"Deleted Hub Contact {contact.id}; no Zoho record was changed.",
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
    mailbox = HubMailboxService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    ).get_view(folder=folder, unread_only=unread, selected_key=selected)
    return templates.TemplateResponse(
        request,
        "emails.html",
        {
            "mailbox": mailbox,
            "linked_case": _mailbox_linked_case(db, mailbox.selected),
            "folder": folder,
            "unread": unread,
            "email_state": email_state if email_state in {"success", "error"} else "",
            "email_message": email_message[:500] if email_state in {"success", "error"} else "",
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/email-templates", response_class=HTMLResponse)
def email_template_management_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    template: str = "",
    state: str = "",
    message: str = "",
):
    """Manage the Hub's locally stored email templates without querying Zoho."""
    _require_hub_admin(request)
    service = _customer_communication_service(db)
    email_templates = service.list_email_templates()
    template_library_templates = tuple(
        {
            "id": email_template.id,
            "name": email_template.name,
            "subject": email_template.subject,
            "module": email_template.module,
            "category": email_template.category or email_template.module or "Weitere Vorlagen",
            "compiler_mode": email_template.compiler_mode,
        }
        for email_template in email_templates
    )
    selected_template = None
    selected_template_id = template.strip()
    if selected_template_id:
        try:
            selected_template = service.get_email_template_source(template_id=selected_template_id)
        except ValueError:
            selected_template_id = ""
    return templates.TemplateResponse(
        request,
        "email_templates.html",
        {
            "template_library_templates": template_library_templates,
            "selected_template": selected_template,
            "selected_template_id": selected_template_id,
            "template_state": state if state in {"success", "error"} else "",
            "template_message": message[:500] if state in {"success", "error"} else "",
            "csrf_token": get_csrf_token(request),
        },
    )


@router.post("/email-templates/{template_id}")
def update_email_template(
    template_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        template = _customer_communication_service(db).update_email_template(
            template_id=template_id,
            name=name,
            subject=subject,
            content=content,
        )
    except ValueError as exc:
        db.rollback()
        query = urlencode({"template": template_id, "state": "error", "message": str(exc)})
        return RedirectResponse(url=f"/email-templates?{query}", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-hub-email-template",
        result="ok",
        detail=f"Updated local email template {template.id}; the template content is not retained in the audit log.",
    )
    db.commit()
    query = urlencode({"template": template.id, "state": "success", "message": "Vorlage wurde im Hub gespeichert."})
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
        template = _customer_communication_service(db).clone_email_template(
            template_id=template_id,
            name=name,
        )
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
        detail=f"Cloned local email template {template_id} as {template.id}; template content is not retained in the audit log.",
    )
    db.commit()
    query = urlencode({"template": template.id, "state": "success", "message": "Vorlage wurde als Kopie angelegt."})
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
        _customer_communication_service(db).delete_email_template(template_id=template_id)
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
    mailbox_service = HubMailboxService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )
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
        source_email = service.source_email(source_email_key=source_email_key)
        if service.linked_case_for_source_email(source_email_key=source_email.key) is not None:
            raise HubCaseError("Diese E-Mail ist bereits mit einem Fall verknüpft.")
    except HubCaseError as exc:
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
        customer_id = int(raw_customer_id) if raw_customer_id else None
        source_email = service.source_email(source_email_key=source_email_key)
        if service.linked_case_for_source_email(source_email_key=source_email.key) is not None:
            raise HubCaseError("Diese E-Mail ist bereits mit einem Fall verknüpft.")
        if source_email.customer_id is not None and customer_id != source_email.customer_id:
            raise HubCaseError("Der Kundenbezug der ausgewählten E-Mail darf beim Anlegen nicht geändert werden.")
        case = service.create_case(
            customer_id=customer_id,
            submitted_values=submitted_values,
            actor_username=user.username,
        )
        service.link_email(case_id=case.id, source_email_key=source_email.key)
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
        detail=f"Created Hub Case {case.id} and linked one selected mailbox email.",
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
        source_email = service.source_email(source_email_key=source_email_key)
        linked_case = service.linked_case_for_source_email(source_email_key=source_email.key)
        if linked_case is None or linked_case.case.id != case_id:
            raise HubCaseError("Der Fall ist nicht mehr mit dieser E-Mail verknüpft.")
        detail = service.get_detail(case_id=case_id)
        if detail is None:
            raise HubCaseError("Der Fall wurde nicht gefunden.")
    except HubCaseError as exc:
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
        customer_id = int(raw_customer_id) if raw_customer_id else None
        source_email = service.source_email(source_email_key=source_email_key)
        linked_case = service.linked_case_for_source_email(source_email_key=source_email.key)
        if linked_case is None or linked_case.case.id != case_id:
            raise HubCaseError("Der Fall ist nicht mehr mit dieser E-Mail verknüpft.")
        if source_email.customer_id is not None and customer_id != source_email.customer_id:
            raise HubCaseError("Der Kundenbezug der ausgewählten E-Mail darf nicht geändert werden.")
        case = service.update_case(
            case_id=case_id,
            customer_id=customer_id,
            submitted_values=submitted_values,
        )
        linked_case = service.linked_case_for_source_email(source_email_key=source_email.key)
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
        detail=f"Updated Hub Case {case.id} from one linked mailbox email.",
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
    service = HubCaseService(db=db, cipher=get_secret_cipher())
    try:
        source_email = service.source_email(source_email_key=source_email_key)
        linked_case = service.linked_case_for_source_email(source_email_key=source_email.key)
        if linked_case is None or linked_case.case.id != case_id:
            raise HubCaseError("Der Fall ist nicht mehr mit dieser E-Mail verknüpft.")
        case = service.delete_case(case_id=case_id)
    except HubCaseError as exc:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-hub-case-from-mailbox-email",
        result="ok",
        detail=f"Deleted Hub Case {case.id} from one linked mailbox email.",
    )
    db.commit()
    return {"case_id": case_id, "source_email_key": source_email.key}


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
    mailbox_service = HubMailboxService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )
    return templates.TemplateResponse(
        request,
        "emails_folder_panel.html",
        {
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
    mailbox = HubMailboxService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    ).get_folder_view(folder=folder, unread_only=unread)
    return templates.TemplateResponse(
        request,
        "emails_message_list.html",
        {
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
    mailbox = HubMailboxService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )
    return {
        "folder_counts": mailbox.get_folder_counts(),
        "unread_count": _unread_email_count_for_db(db),
    }


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
    mailbox = HubMailboxService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )
    try:
        changed_count = mailbox.apply_batch_action(keys=keys or [], action=action)
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
        "folder_counts": mailbox.get_folder_counts(),
        "unread_count": _unread_email_count_for_db(db),
    }


@router.get("/emails/compose/options", response_class=JSONResponse)
def mailbox_compose_options(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    service = _customer_communication_service(db)
    sender_error = ""
    try:
        senders = service.list_senders()
    except ZohoCrmError as exc:
        senders = ()
        sender_error = str(exc)
    templates = service.list_email_templates()
    default_sender_email = next(
        (sender.email for sender in senders if sender.email.casefold() == DEFAULT_HUB_MAILBOX_SENDER_EMAIL),
        senders[0].email if senders else "",
    )
    default_template = next(
        (template for template in templates if template.name.casefold() == "standard_neu"),
        None,
    )
    return {
        "senders": [{"name": sender.name, "email": sender.email} for sender in senders],
        "default_sender_email": default_sender_email,
        "default_template_id": default_template.id if default_template else "",
        "templates": [
            {
                "id": template.id,
                "name": template.name,
                "module": template.module,
                "category": template.category,
                "subject": template.subject,
            }
            for template in templates
        ],
        "sender_error": sender_error,
    }


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
    matches = _customer_communication_service(db).search_recipients(query=q)
    return {
        "recipients": [
            {
                "customer_id": match.customer_id,
                "customer_name": match.customer_name,
                "key": match.recipient.key,
                "name": match.recipient.name,
                "email": match.recipient.email,
            }
            for match in matches
        ]
    }


@router.get("/emails/compose/templates/{template_id}", response_class=JSONResponse)
def mailbox_compose_template_preview(
    template_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Return a local template even before the recipient context is known."""
    _require_hub_admin(request)
    try:
        template = _customer_communication_service(db).get_email_template_preview(template_id=template_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": template.id,
        "name": template.name,
        "subject": template.subject,
        "content": template.content,
        "unresolved_placeholders": template.unresolved_placeholders,
    }


@router.get("/emails/compose/customers/{customer_id}/recipients", response_class=JSONResponse)
def mailbox_compose_customer_contact_recipients(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Return each stored email address of contacts linked to this customer."""
    _require_hub_admin(request)
    customer = db.get(Customer, customer_id)
    if customer is None or not customer.is_visible:
        raise HTTPException(status_code=404, detail="Customer not found.")
    recipients = _customer_communication_service(db).list_contact_recipients(customer_id=customer.id)
    return {
        "recipients": [
            {
                "customer_id": customer.id,
                "customer_name": customer.name,
                "key": recipient.key,
                "name": recipient.name,
                "email": recipient.email,
            }
            for recipient in recipients
        ]
    }


@router.get("/emails/linked/{customer_id}/{email_id}/compose-context", response_class=JSONResponse)
def mailbox_linked_email_compose_context(
    customer_id: int,
    email_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    action: str = "",
):
    """Prepare an editable composer context for an opened mailbox message."""
    _require_hub_admin(request)
    service = _customer_communication_service(db)
    try:
        if action in {"reply", "reply_all"}:
            reply = service.get_email_reply(customer_id=customer_id, email_id=email_id)
            # The reply action may have fetched the original body for the quoted message.
            db.commit()
            return {
                "action": action,
                "customer_id": customer_id,
                "recipient": {
                    "key": reply.recipient_key,
                    "name": reply.recipient_name,
                    "email": reply.recipient_email,
                },
                "subject": reply.subject,
                "content": reply.content,
                "cc_emails": list(reply.reply_all_cc_emails) if action == "reply_all" else [],
                "reply_to_email_id": reply.email_id,
                "forward_from_email_id": None,
            }
        if action == "forward":
            forward = service.get_email_forward(customer_id=customer_id, email_id=email_id)
            # The explicit forwarding action may have fetched a previously missing body.
            db.commit()
            return {
                "action": action,
                "customer_id": customer_id,
                "recipient": None,
                "subject": forward.subject,
                "content": forward.content,
                "cc_emails": [],
                "reply_to_email_id": None,
                "forward_from_email_id": forward.email_id,
            }
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ZohoCrmError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail="Unknown mailbox compose action.")


@router.get("/emails/drafts/{draft_id}/compose-context", response_class=JSONResponse)
def mailbox_draft_compose_context(
    draft_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    try:
        return HubMailboxService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).get_draft_compose_context(draft_id=draft_id)
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
    _require_hub_admin(request)
    try:
        return HubMailboxService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).get_unassigned_email_compose_context(email_id=email_id, action=action)
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
    recipient_name: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    cc_emails: Annotated[str, Form()] = "",
    template_id: Annotated[str, Form()] = "",
    reply_to_email_id: Annotated[str, Form()] = "",
    forward_from_email_id: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        parsed_draft_id = int(draft_id) if draft_id.strip() else None
        parsed_customer_id = int(recipient_customer_id) if recipient_customer_id.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Der Entwurf enthält eine ungültige Zuordnung.") from exc
    mailbox = HubMailboxService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )
    try:
        draft = mailbox.save_draft(
            draft_id=parsed_draft_id,
            sender_email=sender_email,
            recipient_email=recipient_email,
            recipient_key=recipient_key,
            recipient_customer_id=parsed_customer_id,
            recipient_name=recipient_name,
            subject=subject,
            content=content,
            cc_emails=cc_emails,
            template_id=template_id,
            reply_to_email_id=reply_to_email_id,
            forward_from_email_id=forward_from_email_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="save-mailbox-email-draft",
        result="ok",
        detail=f"Saved mailbox email draft {draft.id}; email content is not retained in the audit log.",
    )
    db.commit()
    return {
        "draft_id": draft.id,
        "folder_counts": mailbox.get_folder_counts(),
        "unread_count": _unread_email_count_for_db(db),
    }


@router.post("/emails/send")
async def send_direct_mailbox_email(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    sender_email: Annotated[str, Form()] = "",
    recipient_email: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    cc_emails: Annotated[str, Form()] = "",
    draft_id: Annotated[str, Form()] = "",
    reply_to_email_id: Annotated[str, Form()] = "",
    forward_from_email_id: Annotated[str, Form()] = "",
    attachments: Annotated[list[UploadFile] | None, File()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    """Deliver an email without a customer selection through Mittwald SMTP."""
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    mailbox = HubMailboxService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )
    try:
        reply_to_id = int(reply_to_email_id) if reply_to_email_id.strip() else None
        forward_from_id = int(forward_from_email_id) if forward_from_email_id.strip() else None
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
        if draft_id.strip().isdigit():
            mailbox.discard_draft(draft_id=int(draft_id))
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/emails?{urlencode({'folder': 'sent', 'email_state': 'error', 'email_message': str(exc)})}",
            status_code=303,
        )
    finally:
        for attachment in attachments or []:
            await attachment.close()

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="send-direct-mittwald-email",
        result="ok",
        detail="Sent an unlinked mailbox email through Mittwald; recipients and content are not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(
        url=f"/emails?{urlencode({'folder': 'sent', 'selected': f'unassigned-{sent.id}', 'email_state': 'success', 'email_message': 'E-Mail wurde über Mittwald versendet.'})}",
        status_code=303,
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
        HubMailboxService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).mark_unassigned_read(email_id=email_id)
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
        download = HubMailboxService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).download_unassigned_attachment(email_id=email_id, attachment_id=attachment_id)
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
        _customer_communication_service(db).load_email_content(customer_id=customer_id, email_id=email_id)
    except (ValueError, ZohoCrmError):
        db.rollback()
    else:
        db.commit()
    return RedirectResponse(
        url=_mailbox_url(folder=folder, unread=unread, selected=f"linked-{customer_id}-{email_id}"),
        status_code=303,
    )


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
):
    cipher = get_secret_cipher()
    can_manage_customer_fields = getattr(request.state, "hub_user", None) is not None and request.state.hub_user.role == "admin"
    detail = CustomerDirectoryService(db=db, cipher=cipher).get_detail(
        customer_id=customer_id,
        include_sensitive=can_manage_customer_fields,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    communication_service = CustomerCommunicationService(
        db=db,
        cipher=cipher,
        public_base_url=get_settings().public_base_url,
    )
    can_manage_communications = getattr(request.state, "hub_user", None) is not None and request.state.hub_user.role == "admin"
    communication_view = communication_service.get_view(customer_id=customer_id)
    communication_state = communication if communication in {"success", "warning", "error"} else ""
    activity_state = activity if activity in {"success", "error"} else ""
    berlin_now = datetime.now(ZoneInfo("Europe/Berlin"))
    call_start = suggested_call_start(berlin_now).replace(tzinfo=None)
    task_default_date = _next_task_due_date(berlin_now)
    activity_service = CustomerActivityService(db=db)
    return templates.TemplateResponse(
        request,
        "customer_detail.html",
        {
            "detail": detail,
            "communication": communication_view,
            "communication_state": communication_state,
            "communication_message": message[:500] if communication_state else "",
            "can_manage_communications": can_manage_communications,
            "can_manage_customer_fields": can_manage_customer_fields,
            "fields_state": fields if fields in {"success", "error"} else "",
            "fields_message": fields_message[:500] if fields in {"success", "error"} else "",
            "layout_state": layout if layout in {"success", "error"} else "",
            "layout_message": layout_message[:500] if layout in {"success", "error"} else "",
            "activity_calls": activity_service.list_calls(customer_id=customer_id),
            "activity_tasks": activity_service.list_tasks(customer_id=customer_id),
            "activity_meetings": activity_service.list_meetings(customer_id=customer_id),
            "activity_state": activity_state,
            "activity_message": activity_message[:500] if activity_state else "",
            "call_status_options": CALL_STATUS_OPTIONS,
            "call_direction_options": CALL_DIRECTION_OPTIONS,
            "call_duration_options": CALL_DURATION_OPTIONS,
            "call_time_options": CALL_TIME_OPTIONS,
            "call_reminder_channel_options": CALL_REMINDER_CHANNEL_OPTIONS,
            "call_reminder_options": CALL_REMINDER_OPTIONS,
            "call_defaults": {
                "start_date": call_start.strftime("%Y-%m-%d"),
                "start_time": call_start.strftime("%H:%M"),
                "duration_minutes": 30,
                "reminder_channel": "popup",
                "reminder_minutes_before": 5,
            },
            "task_defaults": {
                "due_date": task_default_date.isoformat(),
                "due_time": "09:00",
                "reminder_channel": "email",
                "reminder_minutes_before": 0,
            },
            "meeting_defaults": {
                "start_date": call_start.strftime("%Y-%m-%d"),
                "start_time": call_start.strftime("%H:%M"),
                "end_date": (call_start + timedelta(minutes=60)).strftime("%Y-%m-%d"),
                "end_time": (call_start + timedelta(minutes=60)).strftime("%H:%M"),
                "duration_minutes": 60,
                "reminder_channel": "popup",
                "reminder_minutes_before": 15,
            },
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
    csrf_token = form.get("csrf_token")
    require_csrf(request, csrf_token if isinstance(csrf_token, str) else "")
    user = _require_hub_admin(request)
    submitted_values = {
        str(key): value
        for key, value in form.multi_items()
        if isinstance(value, str)
    }
    try:
        customer = ZohoCrmService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).update_customer_fields(
            customer_id=customer_id,
            submitted_values=submitted_values,
        )
    except ZohoCrmError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/customers/{customer_id}?{query}#customer-fields", status_code=303)

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-zoho-customer-fields",
        result="ok",
        detail=f"Updated selected Zoho Account fields for {customer.name} ({customer.zoho_id}).",
    )
    db.commit()
    query = urlencode({"fields": "success", "fields_message": "Kundendaten wurden in Zoho CRM gespeichert."})
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
    order_json = form.get("order_json")
    if not isinstance(order_json, str):
        order_json = ""
    detail = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).get_detail(
        customer_id=customer_id,
        include_sensitive=True,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    try:
        ModuleLayoutService(db=db).configure(
            actor=user,
            layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
            item_order_json=order_json,
            allowed_keys=tuple(field.key for field in detail.display_profile_fields),
        )
    except ModuleLayoutError as exc:
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
        result = _customer_communication_service(db).create_note(
            customer_id=customer_id,
            actor=user.username,
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
        result="ok" if result.success else "failed",
        detail=f"Created customer note for customer {customer_id}; note content is not retained in the audit log.",
    )
    db.commit()
    return _customer_communication_redirect(customer_id, "success" if result.success else "warning", result.message)


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
        result = _customer_communication_service(db).update_note(
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
        result="ok" if result.success else "failed",
        detail=f"Updated customer note {note_id} for customer {customer_id}; note content is not retained in the audit log.",
    )
    db.commit()
    return _customer_communication_redirect(customer_id, "success" if result.success else "warning", result.message)


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
        result = _customer_communication_service(db).delete_note(customer_id=customer_id, note_id=note_id)
    except (ValueError, ZohoCrmError) as exc:
        db.rollback()
        return _customer_communication_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-zoho-customer-note",
        result="ok" if result.success else "failed",
        detail=f"Deleted customer note {note_id} for customer {customer_id}.",
    )
    db.commit()
    return _customer_communication_redirect(customer_id, "success" if result.success else "warning", result.message)


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
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        call = CustomerActivityService(db=db).schedule_call(
            customer_id=customer_id,
            actor=user.username,
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
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="schedule-customer-call",
        result="ok",
        detail=f"Scheduled customer call {call.id} for customer {customer_id}; call description is not retained in the audit log.",
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
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        call = CustomerActivityService(db=db).update_call(
            customer_id=customer_id,
            call_id=call_id,
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
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-customer-call",
        result="ok",
        detail=f"Updated customer call {call.id} for customer {customer_id}; call description is not retained in the audit log.",
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
        call = CustomerActivityService(db=db).delete_call(customer_id=customer_id, call_id=call_id)
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-customer-call",
        result="ok",
        detail=f"Deleted customer call {call.id} for customer {customer_id}.",
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
        call = CustomerActivityService(db=db).complete_call(customer_id=customer_id, call_id=call_id)
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="complete-customer-call",
        result="ok",
        detail=f"Completed customer call {call.id} for customer {customer_id}.",
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
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        task = CustomerActivityService(db=db).schedule_task(
            customer_id=customer_id,
            actor=user.username,
            name=name,
            status=status,
            due_date=due_date,
            due_time=due_time,
            reminder_channel=reminder_channel,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="schedule-customer-task",
        result="ok",
        detail=f"Scheduled customer task {task.id} for customer {customer_id}; task description is not retained in the audit log.",
    )
    db.commit()
    TaskEmailReminderWorker.notify_schedule_changed()
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
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        task = CustomerActivityService(db=db).update_task(
            customer_id=customer_id,
            task_id=task_id,
            name=name,
            status=status,
            due_date=due_date,
            due_time=due_time,
            reminder_channel=reminder_channel,
            reminder_minutes_before=reminder_minutes_before,
            description=description,
        )
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-customer-task",
        result="ok",
        detail=f"Updated customer task {task.id} for customer {customer_id}; task description is not retained in the audit log.",
    )
    db.commit()
    TaskEmailReminderWorker.notify_schedule_changed()
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
        task = CustomerActivityService(db=db).delete_task(customer_id=customer_id, task_id=task_id)
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-customer-task",
        result="ok",
        detail=f"Deleted customer task {task.id} for customer {customer_id}.",
    )
    db.commit()
    TaskEmailReminderWorker.notify_schedule_changed()
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
        task = CustomerActivityService(db=db).complete_task(customer_id=customer_id, task_id=task_id)
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="complete-customer-task",
        result="ok",
        detail=f"Completed customer task {task.id} for customer {customer_id}.",
    )
    db.commit()
    TaskEmailReminderWorker.notify_schedule_changed()
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
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        meeting = CustomerActivityService(db=db).schedule_meeting(
            customer_id=customer_id,
            actor=user.username,
            name=name,
            status=status,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels or [],
            reminder_minutes_before=reminder_minutes_before or [],
            description=description,
        )
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="schedule-customer-meeting",
        result="ok",
        detail=f"Scheduled customer meeting {meeting.id} for customer {customer_id}; meeting description is not retained in the audit log.",
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
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        meeting = CustomerActivityService(db=db).update_meeting(
            customer_id=customer_id,
            meeting_id=meeting_id,
            name=name,
            status=status,
            start_date=start_date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            reminder_channels=reminder_channels or [],
            reminder_minutes_before=reminder_minutes_before or [],
            description=description,
        )
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="update-customer-meeting",
        result="ok",
        detail=f"Updated customer meeting {meeting.id} for customer {customer_id}; meeting description is not retained in the audit log.",
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
        meeting = CustomerActivityService(db=db).delete_meeting(customer_id=customer_id, meeting_id=meeting_id)
    except CustomerActivityError as exc:
        db.rollback()
        return _customer_activity_redirect(customer_id, "error", str(exc))

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="delete-customer-meeting",
        result="ok",
        detail=f"Deleted customer meeting {meeting.id} for customer {customer_id}.",
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
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
    template_id: Annotated[str, Form()] = "",
    reply_to_email_id: Annotated[str, Form()] = "",
    cc_emails: Annotated[str, Form()] = "",
    forward_from_email_id: Annotated[str, Form()] = "",
    draft_id: Annotated[str, Form()] = "",
    attachments: Annotated[list[UploadFile] | None, File()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    try:
        reply_to_id = int(reply_to_email_id) if reply_to_email_id.strip() else None
        forward_from_id = int(forward_from_email_id) if forward_from_email_id.strip() else None
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
        )
        if result.success and draft_id.strip().isdigit():
            HubMailboxService(
                db=db,
                cipher=get_secret_cipher(),
                public_base_url=get_settings().public_base_url,
            ).discard_draft(draft_id=int(draft_id))
    except (ValueError, ZohoCrmError) as exc:
        db.rollback()
        return _customer_communication_redirect(customer_id, "error", str(exc))
    finally:
        for attachment in attachments or []:
            await attachment.close()

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="send-customer-email",
        result="ok" if result.success else "failed",
        detail=f"Sent customer email for customer {customer_id}; recipients and message content are not retained in the audit log.",
    )
    db.commit()
    return _customer_communication_redirect(customer_id, "success" if result.success else "warning", result.message)


@router.get("/customers/{customer_id}/communications/email-templates/{template_id}", response_class=JSONResponse)
def load_customer_communication_email_template(
    customer_id: int,
    template_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    recipient_key: str = "",
):
    _require_hub_admin(request)
    try:
        template = _customer_communication_service(db).get_email_template(
            customer_id=customer_id,
            template_id=template_id,
            recipient_key=recipient_key,
        )
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
        _customer_communication_service(db).mark_email_read(customer_id=customer_id, email_id=email_id)
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
        download = _customer_communication_service(db).download_email_attachment(
            customer_id=customer_id,
            email_id=email_id,
            attachment_id=attachment_id,
        )
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
    detail = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).get_contact_detail(
        customer_id=customer_id,
        contact_id=contact_id,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Contact not found for this customer.")
    can_manage_contacts = getattr(request.state, "hub_user", None) is not None and request.state.hub_user.role == "admin"
    return templates.TemplateResponse(
        request,
        "customer_contact_detail.html",
        _contact_detail_context(
            request,
            db,
            detail=detail,
            can_manage_contacts=can_manage_contacts,
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
        contact = ZohoCrmService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).update_contact(
            customer_id=customer_id,
            contact_id=contact_id,
            submitted_values=submitted_values,
        )
    except ZohoCrmError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/customers/{customer_id}/contacts/{contact_id}?{query}#contact-fields", status_code=303)
    write_audit_log(
        db, site=None, actor=user.username, source="hub-web", action="update-zoho-contact-fields", result="ok",
        detail=f"Updated Zoho Contact {contact.zoho_id} for customer {customer_id}.",
    )
    db.commit()
    query = urlencode({"fields": "success", "fields_message": "Kontaktdaten wurden in Zoho CRM gespeichert."})
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
        ModuleLayoutService(db=db).configure(
            actor=user,
            layout_key=CONTACT_FIELDS_LAYOUT_KEY,
            item_order_json=str(form.get("order_json") or ""),
            allowed_keys=tuple(field.key for field in detail.display_profile_fields),
        )
    except ModuleLayoutError as exc:
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
        contact = ZohoCrmService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).synchronize_contact(
            customer_id=customer_id,
            contact_id=contact_id,
        )
    except ZohoCrmError as exc:
        db.rollback()
        query = urlencode({"fields": "error", "fields_message": str(exc)})
        return RedirectResponse(url=f"/customers/{customer_id}/contacts/{contact_id}?{query}#contact-fields", status_code=303)
    write_audit_log(
        db, site=None, actor=user.username, source="hub-web", action="sync-zoho-contact", result="ok",
        detail=f"Synchronized Zoho Contact {contact.zoho_id} for customer {customer_id}.",
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
        customer, site = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).link_exact_match(customer_id=customer_id, site_id=site_id)
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
    refresh_service = FleetRefreshService(db=db)
    run, created = refresh_service.create_run(
        actor=user,
        mode=FleetRefreshService.MODE_FRESH_USERS,
        site_ids=selected_site_ids,
    )
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
    refresh_service = FleetRefreshService(db=db)
    run, created = refresh_service.create_run(
        actor=user,
        mode=FleetRefreshService.MODE_FRESH_BACKUPS,
        site_ids=selected_site_ids,
    )
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
        outcomes = SiteUserService(db=db, cipher=get_secret_cipher()).create_users_bulk(
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
        outcomes = SiteUserService(db=db, cipher=get_secret_cipher()).update_roles_bulk(
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
        outcomes = SiteUserService(db=db, cipher=get_secret_cipher()).update_passwords_bulk(
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
        created = SiteUserService(db=db, cipher=get_secret_cipher()).create_user(
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
        changed = SiteUserService(db=db, cipher=get_secret_cipher()).update_role(
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
        changed = SiteUserService(db=db, cipher=get_secret_cipher()).update_password(
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
        batch = UserDeletionBatchService(db=db, cipher=get_secret_cipher()).prepare_batch(
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
        UserDeletionBatchService(db=db, cipher=get_secret_cipher()).start_batch(
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
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="The deletion batch no longer exists.")
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
        service.cancel_batch(batch_id=batch_id)
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
    all_items = inventory_service.list_items(limit=1000)
    entries = inventory_service.build_update_workbench(all_items)
    selected_site_ids = set(site_id or []) if site_scope == "selected" else None
    filtered_entries = inventory_service.filter_update_workbench(
        entries,
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
    batch_runs = maintenance_service.list_plugin_update_batch(update_batch)
    batch_running = any(run.status == "running" for run in batch_runs)
    if batch_running:
        # Resume a user-started batch if a process restart interrupted polling.
        schedule_pending_direct_updates()
    complete_site_update_run = (
        maintenance_service.get_complete_site_update_run(complete_update_run_id)
        if complete_update_run_id is not None
        else None
    )
    complete_site_update_running = (
        complete_site_update_run is not None
        and complete_site_update_run.status == "running"
    )
    if complete_site_update_running:
        schedule_pending_complete_site_updates()
    fleet_refresh_service = FleetRefreshService(db=db)
    active_fleet_refresh_run = fleet_refresh_service.get_active_run(modes=FleetRefreshService.update_modes())
    progress_refresh_run = (
        fleet_refresh_service.get_run(active_refresh_run_id)
        if active_refresh_run_id is not None
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
    refresh_runs = fleet_refresh_service.list_recent_runs(limit=20, modes=FleetRefreshService.update_history_modes())
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
            "summary": inventory_service.summarize_update_workbench(filtered_entries),
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
    refresh_service = FleetRefreshService(db=db)
    run, created = refresh_service.create_run(
        actor=user,
        mode=FleetRefreshService.MODE_FRESH_UPDATES,
        site_ids=selected_site_ids,
    )
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
    run = FleetRefreshService(db=db).get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="The fleet refresh run no longer exists.")
    return _fleet_refresh_status_payload(run)


@router.get("/updates/direct-update-batches/{batch_id}/status", response_class=JSONResponse)
def direct_update_batch_status(
    batch_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_hub_admin(request)
    runs = MaintenanceRunService(db=db, cipher=get_secret_cipher()).list_plugin_update_batch(batch_id)
    if not runs:
        raise HTTPException(status_code=404, detail="The direct update batch no longer exists.")
    return _direct_update_batch_status_payload(batch_id, runs)


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
        outcome = MaintenanceRunService(db=db, cipher=get_secret_cipher()).cancel_direct_update_batch(
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
    service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
    run = service.get_complete_site_update_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="The complete update workflow no longer exists.")
    return _complete_site_update_status_payload(run, service.complete_site_update_child_runs(run.id))


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
        cancelled = MaintenanceRunService(db=db, cipher=get_secret_cipher()).cancel_complete_site_update(
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
    site_options = sorted(SiteRepository(db).list_sites(limit=1000), key=lambda site: site.domain.casefold())
    selected_site_ids = set(site_id or []) if site_scope == "selected" else None
    maintenance_service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
    batch_runs = maintenance_service.list_plugin_installation_batch(install_batch)
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
    db: Annotated[Session, Depends(get_db)],
    search: str = "",
    browse: str = "popular",
    page: Annotated[int, Query(ge=1, le=100)] = 1,
):
    try:
        catalog = PluginInstallationPackageService(db=db).search_wordpress_org_plugins(
            search=search,
            browse=browse,
            page=page,
        )
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
        outcome = MaintenanceRunService(db=db, cipher=get_secret_cipher()).start_plugin_installations(
            site_ids=selected_site_ids,
            package=package,
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
        outcome = MaintenanceRunService(db=db, cipher=get_secret_cipher()).start_direct_updates(
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
        outcome = MaintenanceRunService(db=db, cipher=get_secret_cipher()).start_complete_site_update(
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
        run, cancelled = service.cancel_run(actor=user, run_id=run_id)
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
            outcome = service.start_direct_updates(selected_keys=selected or [], actor=user.username)
        else:
            outcome = service.start_site_updates(site_id=site_id, selected_keys=selected or [], actor=user.username)
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
    repository = SiteRepository(db)
    site = repository.get_site(site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    maintenance_run_history = MaintenanceRunService(db=db, cipher=get_secret_cipher()).list_site_run_history(site_id)
    user_inventory = SiteUserService(db=db, cipher=get_secret_cipher()).get_latest_inventory(site_id)
    site_entries = [
        entry
        for entry in inventory_service.build_update_workbench(inventory_service.list_items(limit=1000))
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
        state_payload = SiteInventoryService(db=db, cipher=get_secret_cipher()).refresh_site_state(site_id)
        updates_payload = SiteUpdateService(db=db, cipher=get_secret_cipher()).refresh_site_updates(site_id)
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
        payload = SiteBackupService(db=db, cipher=get_secret_cipher()).refresh_site_backup_status(site_id)
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
            snapshot = SiteBackupService(db=db, cipher=get_secret_cipher()).refresh_site_backup_status(site_id)["snapshot"]
            message = (
                f"UpdraftPlus backup list checked: {snapshot.backup_count} backup set(s) are currently reported. "
                "No backup was created, changed, or deleted."
            )
            result = "ok"
        elif backup_action == "delete-selected":
            outcome = service.start_updraftplus_backup_deletion(
                site_id=site_id,
                selections=selected_backup or [],
                actor=user.username,
                deletion_confirmation=deletion_confirmation,
            )
            message = outcome.message
            result = outcome.result
        else:
            outcome = service.start_updraftplus_backup(
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
        inventory = SiteUserService(db=db, cipher=get_secret_cipher()).refresh_site_users(site_id, actor=user.username)
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
        created = SiteUserService(db=db, cipher=get_secret_cipher()).create_user(
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
        changed = SiteUserService(db=db, cipher=get_secret_cipher()).update_password(
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
        SiteUserService(db=db, cipher=get_secret_cipher()).delete_user(
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
        outcome = MaintenanceRunService(db=db, cipher=get_secret_cipher()).start_updraftplus_backup(
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

    site = SiteRepository(db).get_site(site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    if not _is_removable_empty_test_registration(site):
        raise HTTPException(status_code=409, detail="Only empty test registrations can be removed from this screen.")

    domain = site.domain
    db.delete(site)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-web",
        action="remove-empty-test-registration",
        result="ok",
        detail=f"Removed the empty test registration for {domain}.",
    )
    db.commit()
    return RedirectResponse(url=f"/sites?{urlencode({'removed': domain})}", status_code=303)


def _is_removable_empty_test_registration(site) -> bool:
    return (
        site.domain.startswith("test-")
        and site.domain.endswith(".kosmos-medien.de")
        and not site.snapshots
        and not site.update_snapshots
        and not site.backup_snapshots
        and not site.user_snapshots
        and not site.capabilities
        and not site.update_plan_items
    )


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
    entries = service.list_workbench_entries()
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
            for site in service.repository.list_sites(limit=1000)
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
    site_options = sorted(
        (
            site
            for site in repository.list_sites(limit=1000)
            if site.status == "verified"
            and site.customer is not None
            and site.customer.zoho_status in SELECTABLE_CUSTOMER_STATUSES
        ),
        key=lambda site: site.domain.casefold(),
    )
    effective_site_ids = {site.id for site in site_options} if site_scope == "all" else (site_ids or set())
    selected_sites = [site for site in site_options if site.id in effective_site_ids]
    snapshots = repository.get_latest_backup_snapshots_by_site_ids([site.id for site in selected_sites])
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


def _fleet_refresh_status_payload(run) -> dict:
    """Keep the browser payload small while exposing every live progress counter."""
    result = run.result_json or {}
    return {
        "id": run.id,
        "status": run.status,
        "mode": run.mode,
        "error_message": run.error_message,
        "result": {
            "scope": result.get("scope", {}),
            "sites": result.get("sites", {}),
            "updates": result.get("updates", {}),
            "backups": result.get("backups", {}),
            "users": result.get("users", {}),
            "crocoblock": result.get("crocoblock", {}),
            "official_versions": result.get("official_versions", {}),
            "phase": result.get("phase", {}),
            "last_site": result.get("last_site", ""),
            "errors": result.get("errors", []),
        },
    }


def _direct_update_batch_status_payload(batch_id: str, runs: list) -> dict:
    """Expose the small, live status view needed by the direct-update workbench."""
    terminal_statuses = {"succeeded", "failed", "skipped"}

    def batch_position(run) -> int:
        position = (run.result_json or {}).get("batch_position")
        return position if isinstance(position, int) else run.id

    ordered_runs = sorted(
        runs,
        key=lambda run: (batch_position(run), run.id),
    )
    rows = []
    for run in ordered_runs:
        result = run.result_json or {}
        rows.append(
            {
                "id": run.id,
                "site_id": run.site.id,
                "site_domain": run.site.domain,
                "site_home_url": getattr(run.site, "home_url", "") or "",
                "site_admin_launch_supported": (
                    getattr(run.site, "status", "") == "verified"
                    and SiteAdminLaunchService.bridge_supports_launch(getattr(run.site, "bridge_version", None))
                ),
                "update_kind": result.get("update_kind") or "plugin",
                "update_name": result.get("update_name") or result.get("plugin_name") or "Unknown update",
                "current_version": result.get("current_version") or "-",
                "target_version": result.get("target_version") or "-",
                "status": run.status,
                "stage": result.get("stage") or "queued",
                "stage_message": result.get("stage_message", ""),
                "error_message": run.error_message or "",
            }
        )
    return {
        "batch_id": batch_id,
        "total": len(rows),
        "completed": sum(row["status"] in terminal_statuses for row in rows),
        "succeeded": sum(row["status"] == "succeeded" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "skipped": sum(row["status"] == "skipped" for row in rows),
        "cancelled": sum(row["stage"] == "cancelled" for row in rows),
        "cancellation_requested": any(isinstance((run.result_json or {}).get("cancellation"), dict) for run in runs),
        "runs": rows,
    }


def _complete_site_update_status_payload(run, child_runs: list) -> dict:
    result = run.result_json or {}
    events = result.get("events", [])
    events = [event for event in events if isinstance(event, dict)]
    steps = [
        {
            "key": step.step_key,
            "status": step.status,
            "detail": step.detail or "",
        }
        for step in run.steps
    ]
    return {
        "run_id": run.id,
        "site_id": run.site.id,
        "site_domain": run.site.domain,
        "status": run.status,
        "stage": result.get("stage", "queued"),
        "stage_message": result.get("stage_message", ""),
        "workflow_phase": result.get("workflow_phase", "queued"),
        "wave": result.get("wave", 0),
        "max_waves": result.get("max_waves", 0),
        "successful_updates": result.get("successful_updates", 0),
        "failed_updates": result.get("failed_updates", 0),
        "skipped_updates": result.get("skipped_updates", 0),
        "cancellation_requested": isinstance(result.get("cancellation"), dict),
        "completed": run.status in {"succeeded", "failed", "skipped"},
        "events": events,
        "steps": steps,
        "child_updates": [
            {
                "id": child.id,
                "status": child.status,
                "stage": (child.result_json or {}).get("stage", "queued"),
                "update_kind": (child.result_json or {}).get("update_kind", "plugin"),
                "update_name": (child.result_json or {}).get("update_name", "Unknown update"),
                "current_version": (child.result_json or {}).get("current_version", "-"),
                "target_version": (child.result_json or {}).get("target_version", "-"),
                "error_message": child.error_message or "",
            }
            for child in child_runs
        ],
    }


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
    customers = tuple(entry.customer for entry in directory.list_entries())
    selected_customer = next((customer for customer in customers if customer.id == selected_customer_id), None)
    return {
        "customers": customers,
        "fields": ZOHO_CONTACT_FIELDS,
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
    values = service.new_form_values()
    values.update(submitted_values or {})
    return {
        "fields": HUB_CASE_FIELDS,
        "customers": service.list_linkable_customers(),
        "selected_customer_id": selected_customer_id,
        "source_email": source_email,
        "submitted_values": values,
        "error": error,
        "csrf_token": get_csrf_token(request),
    }


def _mailbox_linked_case(db: Session, message: object):
    """Resolve the optional case badge for a rendered mailbox message."""
    if message is None or getattr(message, "kind", "") in {"draft", "system"}:
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
    return {
        "detail": detail,
        "customers": service.list_linkable_customers(),
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


def _contact_detail_context(
    request: Request,
    db: Session,
    *,
    detail: object,
    can_manage_contacts: bool,
    fields: str,
    fields_message: str,
    layout: str,
    layout_message: str,
) -> dict[str, object]:
    directory = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    return {
        "detail": detail,
        "can_manage_contacts": can_manage_contacts,
        "linkable_customers": tuple(entry.customer for entry in directory.list_entries()),
        "fields_state": fields if fields in {"success", "error"} else "",
        "fields_message": fields_message[:500] if fields in {"success", "error"} else "",
        "layout_state": layout if layout in {"success", "error"} else "",
        "layout_message": layout_message[:500] if layout in {"success", "error"} else "",
        "csrf_token": get_csrf_token(request),
    }


def _customer_communication_redirect(customer_id: int, state: str, message: str) -> RedirectResponse:
    query = urlencode({"communication": state, "message": message[:500]})
    return RedirectResponse(url=f"/customers/{customer_id}?{query}#customer-communications", status_code=303)


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
        raise HTTPException(status_code=403, detail="Only Hub administrators can manage WordPress users.")
    return user
