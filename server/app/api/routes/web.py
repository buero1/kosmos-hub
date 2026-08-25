from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
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
from app.services.update_plans import UpdatePlanService

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


@router.get("/updates", response_class=HTMLResponse)
def update_workbench_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    kind: Literal["all", "wordpress", "plugin", "theme"] = "all",
    activity: Literal["all", "active", "inactive"] = "all",
):
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
    all_items = inventory_service.list_items(limit=200)
    entries = inventory_service.build_update_workbench(all_items)
    filtered_entries = inventory_service.filter_update_workbench(
        entries,
        query=q,
        kind=kind,
        activity=activity,
    )
    matching_sites = inventory_service.filter_items(all_items, query=q) if q.strip() else []
    return templates.TemplateResponse(
        request,
        "updates.html",
        {
            "entries": filtered_entries,
            "summary": inventory_service.summarize_update_workbench(entries),
            "filters": {"q": q, "kind": kind, "activity": activity},
            "csrf_token": get_csrf_token(request),
            "matching_sites": matching_sites,
        },
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
def update_plan_detail_page(plan_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    service = UpdatePlanService(db=db, cipher=get_secret_cipher())
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Update plan not found.")
    return templates.TemplateResponse(
        request,
        "update_plan_detail.html",
        {
            "plan": plan,
            "preflight": service.build_preflight(plan),
        },
    )


@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail_page(site_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    repository = SiteRepository(db)
    site = repository.get_site(site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    inventory_service = FleetInventoryService(db=db, cipher=get_secret_cipher())
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
