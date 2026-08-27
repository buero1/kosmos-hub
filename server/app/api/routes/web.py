from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.templating import Jinja2Templates

from app.api.routes.accounts import get_csrf_token, require_csrf
from app.core.security import get_secret_cipher
from app.db.session import get_db
from app.repositories.site_repository import SiteRepository
from app.schemas.dashboard import DashboardSummary
from app.services.fleet_inventory import FleetInventoryService
from app.services.audit import write_audit_log
from app.services.site_inventory import SiteInventoryService
from app.services.site_backups import SiteBackupService
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService
from app.services.maintenance_runs import MaintenanceRunService
from app.services.maintenance_worker import schedule_pending_direct_updates
from app.services.fleet_refresh import FleetRefreshService
from app.services.fleet_refresh_settings import FleetRefreshSettingsService
from app.services.update_plans import UpdatePlanService
from app.services.customer_directory import CustomerDirectoryService
from app.services.zoho_crm import ZOHO_RELEVANT_ACCOUNT_STATUSES

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Annotated[Session, Depends(get_db)]):
    repository = SiteRepository(db)
    summary = DashboardSummary.model_validate(repository.get_dashboard_summary())
    latest_sites = repository.list_sites(limit=10)
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    inventory_summary = inventory_service.summarize(inventory_service.list_items(limit=200))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "summary": summary,
            "inventory_summary": inventory_summary,
            "sites": latest_sites,
        },
    )


@router.get("/sites", response_class=HTMLResponse)
def sites_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    plugin: str = "",
    status: str = "",
    inventory: Literal["all", "present", "missing"] = "all",
    updates: Literal["all", "available", "wordpress", "plugins", "themes", "none", "missing"] = "all",
    update_plugin: str = "",
    wordpress: str = "",
    bridge: str = "",
):
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    all_items = inventory_service.list_items(limit=200)
    items = inventory_service.filter_items(
        all_items,
        query=q,
        plugin=plugin,
        status=status,
        inventory_state=inventory,
        updates_state=updates,
        update_plugin=update_plugin,
        wordpress_version=wordpress,
        bridge_version=bridge,
    )
    return templates.TemplateResponse(
        request,
        "sites.html",
        {
            "items": items,
            "inventory_summary": inventory_service.summarize(all_items),
            "filters": {
                "q": q,
                "plugin": plugin,
                "status": status,
                "inventory": inventory,
                "updates": updates,
                "update_plugin": update_plugin,
                "wordpress": wordpress,
                "bridge": bridge,
            },
            "filter_options": {
                "statuses": sorted({item.site.status for item in all_items}),
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


@router.get("/customers", response_class=HTMLResponse)
def customers_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    status: str = "all",
):
    valid_statuses = {*ZOHO_RELEVANT_ACCOUNT_STATUSES, "all"}
    if status not in valid_statuses:
        raise HTTPException(status_code=422, detail="Unknown customer status filter.")
    service = CustomerDirectoryService(db=db, cipher=get_secret_cipher())
    entries = service.list_entries(query=q, status=None if status == "all" else status)
    candidate_count = sum(entry.exact_match_candidate is not None for entry in entries)
    return templates.TemplateResponse(
        request,
        "customers.html",
        {
            "entries": entries,
            "candidate_count": candidate_count,
            "filters": {"q": q, "status": status},
            "status_options": ZOHO_RELEVANT_ACCOUNT_STATUSES,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail_page(
    customer_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    detail = CustomerDirectoryService(db=db, cipher=get_secret_cipher()).get_detail(customer_id=customer_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return templates.TemplateResponse(
        request,
        "customer_detail.html",
        {
            "detail": detail,
            "csrf_token": get_csrf_token(request),
        },
    )


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


@router.get("/updates", response_class=HTMLResponse)
def update_workbench_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    site_id: str = "",
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
    direct_update: str = "",
    official_versions: str = "",
    refresh_run: int | None = None,
    message: str = "",
):
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    all_items = inventory_service.list_items(limit=200)
    entries = inventory_service.build_update_workbench(all_items)
    selected_site_id = int(site_id) if site_id.isdigit() else None
    filtered_entries = inventory_service.filter_update_workbench(
        entries,
        query=q,
        kind=kind,
        activity=activity,
        diagnosis=diagnosis,
        site_id=selected_site_id,
        plugin_identifier=plugin,
    )
    matching_items = inventory_service.filter_items(all_items, query=q) if q.strip() else all_items
    if selected_site_id is not None:
        matching_items = [item for item in matching_items if item.site.id == selected_site_id]
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
    fleet_refresh_service = FleetRefreshService(db=db)
    fleet_refresh_run = fleet_refresh_service.get_run(refresh_run) if refresh_run else None
    refresh_settings = FleetRefreshSettingsService(db=db).get_runtime_settings()
    return templates.TemplateResponse(
        request,
        "updates.html",
        {
            "entries": filtered_entries,
            "summary": inventory_service.summarize_update_workbench(entries),
            "filters": {
                "q": q,
                "site_id": selected_site_id,
                "plugin": plugin,
                "kind": kind,
                "activity": activity,
                "diagnosis": diagnosis,
            },
            "site_options": site_options,
            "plugin_options": plugin_options,
            "csrf_token": get_csrf_token(request),
            "matching_sites": matching_sites,
            "update_batch": update_batch if batch_runs else "",
            "batch_runs": batch_runs,
            "batch_running": batch_running,
            "direct_update": direct_update,
            "official_versions": official_versions,
            "refresh_run": fleet_refresh_run,
            "refresh_settings": refresh_settings,
            "message": message,
        },
    )


@router.post("/updates/refresh-official-plugin-versions")
def refresh_official_plugin_versions(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    mode: Annotated[Literal["normal", "full"], Form()] = "normal",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    fleet_refresh_service = FleetRefreshService(db=db)
    run, created = fleet_refresh_service.create_run(actor=user, mode=mode)
    db.commit()
    if created:
        background_tasks.add_task(FleetRefreshService.process_run, run.id)
        message = "The background refresh was queued. This page will update automatically."
    else:
        message = "A fleet refresh is already running. This page will show its progress."
    return RedirectResponse(
        url=f"/updates?{urlencode({'refresh_run': run.id, 'message': message})}",
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


@router.get("/update-plans", response_class=HTMLResponse)
def update_plans_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    service = UpdatePlanService(db=db, cipher=get_secret_cipher())
    return templates.TemplateResponse(
        request,
        "update_plans.html",
        {
            "plans": service.list_plans(),
        },
    )


@router.post("/update-plans")
def create_update_plan(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    selected: Annotated[list[str] | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    service = UpdatePlanService(db=db, cipher=get_secret_cipher())
    created_by = user.username
    try:
        plan = service.create_draft(
            name=name,
            notes=notes,
            selected_keys=selected or [],
            created_by=created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/update-plans/{plan.id}", status_code=303)


@router.get("/update-plans/{plan_id}", response_class=HTMLResponse)
def update_plan_detail_page(
    plan_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    action: str = "",
    message: str = "",
):
    service = UpdatePlanService(db=db, cipher=get_secret_cipher())
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Update plan not found.")
    preflight = service.build_preflight(plan)
    postflight = service.get_latest_postflight(plan)
    scope_error = service.plugin_update_scope_error(plan)
    recovery_scope_error = service.plugin_recovery_scope_error(plan)
    preflight_ready = len(preflight) == 1 and preflight[0].execution_ready
    return templates.TemplateResponse(
        request,
        "update_plan_detail.html",
        {
            "plan": plan,
            "preflight": preflight,
            "postflight": postflight,
            "csrf_token": get_csrf_token(request),
            "scope_error": scope_error,
            "recovery_scope_error": recovery_scope_error,
            "can_approve": plan.status == "draft" and scope_error is None and preflight_ready,
            "can_execute": plan.status == "approved" and scope_error is None and preflight_ready,
            "can_recover": plan.status == "failed" and recovery_scope_error is None,
            "action_result": action,
            "action_message": message,
        },
    )


@router.post("/update-plans/{plan_id}/approve-plugin-update")
def approve_plugin_update_plan(
    plan_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    service = UpdatePlanService(db=db, cipher=get_secret_cipher())
    try:
        outcome = service.approve_plugin_update(plan_id=plan_id, actor=user.username)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/update-plans/{plan_id}?{urlencode({'action': outcome.result, 'message': outcome.message})}",
        status_code=303,
    )


@router.post("/update-plans/{plan_id}/execute-plugin-update")
def execute_plugin_update_plan(
    plan_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    service = UpdatePlanService(db=db, cipher=get_secret_cipher())
    try:
        outcome = service.execute_plugin_update(plan_id=plan_id, actor=user.username)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/update-plans/{plan_id}?{urlencode({'action': outcome.result, 'message': outcome.message})}",
        status_code=303,
    )


@router.post("/update-plans/{plan_id}/recover-plugin-activation")
def recover_plugin_activation(
    plan_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    service = UpdatePlanService(db=db, cipher=get_secret_cipher())
    try:
        outcome = service.recover_plugin_activation(plan_id=plan_id, actor=user.username)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/update-plans/{plan_id}?{urlencode({'action': outcome.result, 'message': outcome.message})}",
        status_code=303,
    )


@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail_page(site_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    repository = SiteRepository(db)
    site = repository.get_site(site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    maintenance_runs = MaintenanceRunService(db=db, cipher=get_secret_cipher()).list_site_runs(site_id)
    site_entries = [
        entry
        for entry in inventory_service.build_update_workbench(inventory_service.list_items(limit=200))
        if entry.site.id == site.id
    ]
    return templates.TemplateResponse(
        request,
        "site_detail.html",
        {
            "site": site,
            "update_entries": site_entries,
            "csrf_token": get_csrf_token(request),
            "maintenance_runs": maintenance_runs,
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
        and not site.capabilities
        and not site.update_plan_items
    )
