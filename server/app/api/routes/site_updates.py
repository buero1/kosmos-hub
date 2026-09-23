from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import get_db
from app.schemas.updates import SiteUpdateRefreshResponse, SiteUpdateSnapshotResponse
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.hub_operations import HubOperationService, HubOperationError
from app.services.wordpress_remote_catalog import execute_ui_remote
from app.services.wordpress_readers import update_snapshot

router = APIRouter(prefix="/api/v1/sites/{site_id}/updates", tags=["site-updates"])


@router.get("/latest", response_model=SiteUpdateSnapshotResponse | None)
def get_latest_site_updates(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteUpdateSnapshotResponse | None:
    try:
        snapshot = update_snapshot(_gateway(request, db, cipher), site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteUpdateSnapshotResponse.model_validate(snapshot) if snapshot else None


@router.post("/refresh", response_model=SiteUpdateRefreshResponse)
def refresh_site_updates(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> SiteUpdateRefreshResponse:
    try:
        payload = execute_ui_remote(_gateway(request, db, cipher), "wordpress.updates.refresh", site_id=site_id)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return SiteUpdateRefreshResponse(
        site_id=payload["site_id"],
        refreshed_at=payload["refreshed_at"],
        snapshot=SiteUpdateSnapshotResponse.model_validate(payload["snapshot"]),
    )


def _gateway(request, db, cipher):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return HubOperationService(db=db, cipher=cipher, actor=user.username)
