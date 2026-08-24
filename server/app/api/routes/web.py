from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.templating import Jinja2Templates

from app.core.security import get_secret_cipher
from app.db.session import get_db
from app.repositories.site_repository import SiteRepository
from app.schemas.dashboard import DashboardSummary
from app.services.fleet_inventory import FleetInventoryService

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


@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail_page(site_id: int, request: Request, db: Annotated[Session, Depends(get_db)]):
    repository = SiteRepository(db)
    site = repository.get_site(site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return templates.TemplateResponse(
        request,
        "site_detail.html",
        {
            "site": site,
        },
    )
