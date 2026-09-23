from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
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
from app.services.hub_operations import HubOperationService, HubOperationError
from app.services.wordpress_remote_catalog import execute_ui_remote
from app.services.wordpress_workbench import stored_capabilities, state_snapshot
from app.services.site_mcp_proxy import SiteMcpProxyError

router = APIRouter(prefix="/api/v1/sites/{site_id}/inventory", tags=["site-inventory"])


@router.get("/capabilities", response_model=SiteCapabilityInventoryResponse)
def list_site_capabilities(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteCapabilityInventoryResponse:
    try:
        items = stored_capabilities(_gateway(request, db, cipher), site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteCapabilityInventoryResponse(
        items=[StoredSiteCapabilityResponse.model_validate(item) for item in items]
    )


@router.post("/refresh", response_model=SiteInventoryRefreshResponse)
def refresh_site_inventory(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteInventoryRefreshResponse:
    try:
        payload = execute_ui_remote(_gateway(request, db, cipher), "wordpress.capabilities.refresh", site_id=site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
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
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteStateSnapshotResponse | None:
    try:
        snapshot = state_snapshot(_gateway(request, db, cipher), site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    if snapshot is None:
        return None
    return SiteStateSnapshotResponse.model_validate(snapshot)


@router.post("/state/refresh", response_model=SiteStateRefreshResponse)
def refresh_site_state(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteStateRefreshResponse:
    try:
        payload = execute_ui_remote(_gateway(request, db, cipher), "wordpress.inventory.refresh", site_id=site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteStateRefreshResponse(
        site_id=payload["site_id"],
        refreshed_at=payload["refreshed_at"],
        snapshot=SiteStateSnapshotResponse.model_validate(payload["snapshot"]),
    )


def _gateway(request, db, cipher):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return HubOperationService(db=db, cipher=cipher, actor=user.username)
