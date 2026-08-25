from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import get_db
from app.schemas.updates import SiteUpdateRefreshResponse, SiteUpdateSnapshotResponse
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService

router = APIRouter(prefix="/api/v1/sites/{site_id}/updates", tags=["site-updates"])


@router.get("/latest", response_model=SiteUpdateSnapshotResponse | None)
def get_latest_site_updates(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteUpdateSnapshotResponse | None:
    service = SiteUpdateService(db=db, cipher=cipher)
    try:
        snapshot = service.get_latest_site_update_snapshot(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteUpdateSnapshotResponse.model_validate(snapshot) if snapshot else None


@router.post("/refresh", response_model=SiteUpdateRefreshResponse)
def refresh_site_updates(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteUpdateRefreshResponse:
    service = SiteUpdateService(db=db, cipher=cipher)
    try:
        payload = service.refresh_site_updates(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteUpdateRefreshResponse(
        site_id=payload["site_id"],
        refreshed_at=payload["refreshed_at"],
        snapshot=SiteUpdateSnapshotResponse.model_validate(payload["snapshot"]),
    )
