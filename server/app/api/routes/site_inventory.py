from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import get_db
from app.schemas.inventory import (
    SiteCapabilityInventoryResponse,
    SiteInventoryRefreshResponse,
    SiteStateRefreshResponse,
    SiteStateSnapshotResponse,
    StoredSiteCapabilityResponse,
)
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError

router = APIRouter(prefix="/api/v1/sites/{site_id}/inventory", tags=["site-inventory"])


@router.get("/capabilities", response_model=SiteCapabilityInventoryResponse)
def list_site_capabilities(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteCapabilityInventoryResponse:
    service = SiteInventoryService(db=db, cipher=cipher)
    try:
        items = service.list_site_capabilities(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteCapabilityInventoryResponse(
        items=[StoredSiteCapabilityResponse.model_validate(item) for item in items]
    )


@router.post("/refresh", response_model=SiteInventoryRefreshResponse)
def refresh_site_inventory(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteInventoryRefreshResponse:
    service = SiteInventoryService(db=db, cipher=cipher)
    try:
        payload = service.refresh_site_inventory(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteInventoryRefreshResponse(
        site_id=payload["site_id"],
        provider=payload["provider"],
        refreshed_at=payload["refreshed_at"],
        items=[StoredSiteCapabilityResponse.model_validate(item) for item in payload["items"]],
    )


@router.get("/state/latest", response_model=SiteStateSnapshotResponse | None)
def get_latest_site_snapshot(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteStateSnapshotResponse | None:
    service = SiteInventoryService(db=db, cipher=cipher)
    try:
        snapshot = service.get_latest_site_snapshot(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    if snapshot is None:
        return None
    return SiteStateSnapshotResponse.model_validate(snapshot)


@router.post("/state/refresh", response_model=SiteStateRefreshResponse)
def refresh_site_state(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteStateRefreshResponse:
    service = SiteInventoryService(db=db, cipher=cipher)
    try:
        payload = service.refresh_site_state(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteStateRefreshResponse(
        site_id=payload["site_id"],
        refreshed_at=payload["refreshed_at"],
        snapshot=SiteStateSnapshotResponse.model_validate(payload["snapshot"]),
    )
