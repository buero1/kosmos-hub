from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
from app.services.maintenance_worker import schedule_pending_direct_updates
from app.services.fleet_refresh import FleetRefreshService
from app.services.fleet_refresh_settings import FleetRefreshSettingsService
from app.services.update_plans import UpdatePlanService
from app.services.customer_directory import CustomerDirectoryService
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
    role: str = "all",
    customer_status: str = "all",
):
    _require_hub_admin(request)
    selected_site_ids = set(site_id or [])
    return templates.TemplateResponse(
        request,
        "users.html",
        _user_workbench_context(
            request,
            db,
            query=q,
            site_ids=selected_site_ids,
            role=role,
            customer_status=customer_status,
        ),
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
    all_items = inventory_service.list_items(limit=1000)
    entries = inventory_service.build_update_workbench(all_items)
    selected_site_ids = set(site_id or [])
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
    if selected_site_ids:
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
    fleet_refresh_service = FleetRefreshService(db=db)
    fleet_refresh_run = fleet_refresh_service.get_run(refresh_run) if refresh_run else fleet_refresh_service.get_latest_run()
    refresh_settings = FleetRefreshSettingsService(db=db).get_runtime_settings()
    return templates.TemplateResponse(
        request,
        "updates.html",
        {
            "entries": filtered_entries,
            "summary": inventory_service.summarize_update_workbench(entries),
            "filters": {
                "q": q,
                "site_ids": sorted(selected_site_ids),
                "plugin": plugin,
                "kind": kind,
                "activity": activity,
                "diagnosis": diagnosis,
            },
            "site_options": site_options,
            "site_selector": _site_selector_context(
                action="/updates",
                sites=site_options,
                selected_site_ids=selected_site_ids,
                submit_label="Show updates",
                preserved_filters={
                    "q": q,
                    "plugin": plugin,
                    "kind": kind,
                    "activity": activity,
                    "diagnosis": diagnosis,
                },
            ),
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
            "maintenance_runs": maintenance_runs,
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
    role: str = "all",
    customer_status: str = "all",
    error: str = "",
    outcomes: list[dict[str, str]] | None = None,
    action_label: str = "",
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
    selected_site_ids = site_ids or ({site_id} if site_id is not None else set())
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
            "site_ids": sorted(selected_site_ids),
            "role": role,
            "customer_status": customer_status,
        },
        "site_options": site_options,
        "site_selector": _site_selector_context(
            action="/users",
            sites=site_options,
            selected_site_ids=selected_site_ids,
            submit_label="Show users",
            preserved_filters={
                "q": query,
                "role": role,
                "customer_status": customer_status,
            },
        ),
        "role_options": SiteUserService.ROLE_OPTIONS,
        "customer_status_options": status_options,
        "csrf_token": get_csrf_token(request),
        "error": error,
        "outcomes": outcome_rows,
        "action_label": action_label,
        "bulk_limit": SiteUserService.BULK_ACTION_LIMIT,
    }


def _site_selector_context(
    *,
    action: str,
    sites: list,
    selected_site_ids: set[int],
    submit_label: str,
    preserved_filters: dict[str, str],
) -> dict:
    """Build one reusable domain/customer selector for fleet workbenches."""
    customers: dict[int, dict] = {}
    for site in sites:
        customer = site.customer
        if customer is None:
            continue
        customer_entry = customers.setdefault(
            customer.id,
            {
                "id": customer.id,
                "name": customer.name,
                "status": customer.zoho_status or "",
                "site_ids": [],
            },
        )
        customer_entry["site_ids"].append(site.id)

    return {
        "action": action,
        "sites": sites,
        "customers": sorted(customers.values(), key=lambda entry: entry["name"].casefold()),
        "selected_site_ids": selected_site_ids,
        "submit_label": submit_label,
        "preserved_filters": [
            {"name": name, "value": value}
            for name, value in preserved_filters.items()
            if value not in {"", "all"}
        ],
    }


def _site_users_redirect(site_id: int, result: str, message: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/sites/{site_id}?{urlencode({'users': result, 'message': message})}#users",
        status_code=303,
    )


def _require_hub_admin(request: Request):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only Hub administrators can manage WordPress users.")
    return user
