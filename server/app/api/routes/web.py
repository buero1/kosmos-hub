from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.routes.accounts import get_csrf_token, require_csrf
from app.core.security import get_secret_cipher
from app.core.templates import create_templates
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
from app.services.maintenance_runs import MaintenanceRunService
from app.services.maintenance_worker import schedule_pending_complete_site_updates, schedule_pending_direct_updates
from app.services.fleet_refresh import FleetRefreshService
from app.services.update_plans import UpdatePlanService
from app.services.customer_directory import CustomerDirectoryService
from app.services.site_selection import SELECTABLE_CUSTOMER_STATUSES, build_site_selector_context
from app.services.plugin_installation_packages import PluginInstallationPackageService, PluginPackageError
from app.services.zoho_crm import ZOHO_RELEVANT_ACCOUNT_STATUSES

templates = create_templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(include_in_schema=False)


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
    confirm_shared_password: Annotated[bool, Form()] = False,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    if not confirm_shared_password:
        return _render_user_workbench(request, db, error="Confirm that the same password should be applied to every selected WordPress user.")
    try:
        outcomes = SiteUserService(db=db, cipher=get_secret_cipher()).update_passwords_bulk(
            selected_keys=selected or [],
            password=password,
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return _render_user_workbench(request, db, error=str(exc))
    return _render_user_workbench(request, db, outcomes=outcomes, action_label="Password update")


@router.post("/users/bulk/delete/review", response_class=HTMLResponse)
def review_selected_user_deletions(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    selected: Annotated[list[str] | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    _require_hub_admin(request)
    service = SiteUserService(db=db, cipher=get_secret_cipher())
    try:
        targets = service.selected_workbench_entries(selected or [])
    except ValueError as exc:
        return _render_user_workbench(request, db, error=str(exc))

    entries_by_site: dict[int, list] = {}
    for entry in service.list_workbench_entries():
        entries_by_site.setdefault(entry.site.id, []).append(entry)
    review_items = [
        {
            "target": target,
            "replacements": [
                entry for entry in entries_by_site.get(target.site.id, []) if entry.user["id"] != target.user["id"]
            ],
        }
        for target in targets
    ]
    return templates.TemplateResponse(
        request,
        "user_delete_review.html",
        {
            "review_items": review_items,
            "deletion_ready": all(item["replacements"] for item in review_items),
            "csrf_token": get_csrf_token(request),
        },
    )


@router.post("/users/bulk/delete", response_class=HTMLResponse)
def delete_selected_users(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    selected: Annotated[list[str] | None, Form()] = None,
    reassign_to_user_id: Annotated[list[int] | None, Form()] = None,
    confirmation: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_hub_admin(request)
    selection = selected or []
    expected_confirmation = f"DELETE {len(selection)} USERS"
    if confirmation.strip() != expected_confirmation:
        return _render_user_workbench(request, db, error=f'Enter "{expected_confirmation}" to confirm the selected deletion.')
    try:
        outcomes = SiteUserService(db=db, cipher=get_secret_cipher()).delete_users_bulk(
            selected_keys=selection,
            reassign_to_user_ids=reassign_to_user_id or [],
            actor=user.username,
        )
    except (SiteMcpProxyError, ValueError) as exc:
        return _render_user_workbench(request, db, error=str(exc))
    return _render_user_workbench(request, db, outcomes=outcomes, action_label="User deletion")


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
                submit_label="Show selected sites",
            ),
            "selected_site_count": len(site_options) if site_scope == "all" else len(selected_site_ids or set()),
            "install_batch": install_batch if batch_runs else "",
            "batch_runs": batch_runs,
            "plugin_install": plugin_install,
            "message": message,
            "csrf_token": get_csrf_token(request),
        },
    )


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


def _render_user_workbench(
    request: Request,
    db: Session,
    *,
    error: str = "",
    outcomes: list[dict[str, str]] | None = None,
    action_label: str = "",
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
        "fresh_users": fresh_users,
        "message": message,
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


def _require_hub_admin(request: Request):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only Hub administrators can manage WordPress users.")
    return user
