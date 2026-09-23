from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import get_db
from app.schemas.ability import (
    AbilityInfoResponse,
    DiscoverAbilitiesResponse,
    ExecuteAbilityRequest,
    ExecuteAbilityResponse,
)
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService
from app.services.hub_operations import HubOperationService, HubOperationError
from app.services.hub_operation_websites import website_site

router = APIRouter(prefix="/api/v1/sites/{site_id}", tags=["site-abilities"])


@router.get("/abilities", response_model=DiscoverAbilitiesResponse)
def discover_site_abilities(
    site_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> DiscoverAbilitiesResponse:
    _authorize_diagnostics(request, db, cipher, site_id)
    service = SiteMcpProxyService(db=db, cipher=cipher)
    try:
        payload = service.discover_abilities(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return DiscoverAbilitiesResponse.model_validate(payload)


@router.get("/ability-info", response_model=AbilityInfoResponse)
def get_site_ability_info(
    site_id: int,
    request: Request,
    ability_name: str,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> AbilityInfoResponse:
    _authorize_diagnostics(request, db, cipher, site_id)
    service = SiteMcpProxyService(db=db, cipher=cipher)
    try:
        payload = service.get_ability_info(site_id, ability_name)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return AbilityInfoResponse.model_validate(payload)


@router.post("/abilities/execute", response_model=ExecuteAbilityResponse)
def execute_site_ability(
    site_id: int,
    request: Request,
    body: ExecuteAbilityRequest,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> ExecuteAbilityResponse:
    _authorize_diagnostics(request, db, cipher, site_id)
    service = SiteMcpProxyService(db=db, cipher=cipher)
    try:
        payload = service.execute_readonly_ability(site_id, body.ability_name, body.input)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return ExecuteAbilityResponse.model_validate(payload)


def _authorize_diagnostics(request, db, cipher, site_id):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        website_site(HubOperationService(db=db, cipher=cipher, actor=user.username), site_id, action="manage")
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
