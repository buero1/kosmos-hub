from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.site_repository import SiteRepository
from app.schemas.site import SiteDetailResponse, SiteListResponse

router = APIRouter(prefix="/api/v1/sites", tags=["sites"])


@router.get("", response_model=SiteListResponse)
def list_sites(db: Annotated[Session, Depends(get_db)]) -> SiteListResponse:
    repository = SiteRepository(db)
    sites = repository.list_sites()
    return SiteListResponse(items=[SiteDetailResponse.model_validate(site) for site in sites])


@router.get("/{site_id}", response_model=SiteDetailResponse)
def get_site(site_id: int, db: Annotated[Session, Depends(get_db)]) -> SiteDetailResponse:
    repository = SiteRepository(db)
    site = repository.get_site(site_id)
    if site is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found.")
    return SiteDetailResponse.model_validate(site)

