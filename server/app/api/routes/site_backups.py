from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import get_db
from app.schemas.backups import SiteBackupRefreshResponse, SiteBackupSnapshotResponse
from app.services.site_backups import SiteBackupService
from app.services.site_mcp_proxy import SiteMcpProxyError

router = APIRouter(prefix="/api/v1/sites/{site_id}/backups", tags=["site-backups"])


@router.get("/latest", response_model=SiteBackupSnapshotResponse | None)
def get_latest_site_backup_status(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteBackupSnapshotResponse | None:
    service = SiteBackupService(db=db, cipher=cipher)
    try:
        snapshot = service.get_latest_site_backup_snapshot(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteBackupSnapshotResponse.model_validate(snapshot) if snapshot else None


@router.post("/refresh", response_model=SiteBackupRefreshResponse)
def refresh_site_backup_status(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteBackupRefreshResponse:
    service = SiteBackupService(db=db, cipher=cipher)
    try:
        payload = service.refresh_site_backup_status(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteBackupRefreshResponse(
        site_id=payload["site_id"],
        refreshed_at=payload["refreshed_at"],
        snapshot=SiteBackupSnapshotResponse.model_validate(payload["snapshot"]),
    )
