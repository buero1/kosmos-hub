from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.hub_operations import HubOperationError, HubOperationService
from app.services.hub_operation_websites import website_sites, website_site
from app.schemas.site import SiteDetailResponse, SiteListResponse

router = APIRouter(prefix="/api/v1/sites", tags=["sites"])


@router.get("", response_model=SiteListResponse)
def list_sites(request: Request, db: Annotated[Session, Depends(get_db)]) -> SiteListResponse:
    try:
        sites = website_sites(_gateway(request, db), limit=1000)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return SiteListResponse(items=[SiteDetailResponse.model_validate(site) for site in sites])


@router.get("/{site_id}", response_model=SiteDetailResponse)
def get_site(site_id: int, request: Request, db: Annotated[Session, Depends(get_db)]) -> SiteDetailResponse:
    try:
        site = website_site(_gateway(request, db), site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return SiteDetailResponse.model_validate(site)


def _gateway(request, db):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return HubOperationService(db=db, cipher=None, actor=user.username)
