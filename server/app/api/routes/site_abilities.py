from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
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

router = APIRouter(prefix="/api/v1/sites/{site_id}", tags=["site-abilities"])


@router.get("/abilities", response_model=DiscoverAbilitiesResponse)
def discover_site_abilities(
    site_id: int,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> DiscoverAbilitiesResponse:
    service = SiteMcpProxyService(db=db, cipher=cipher)
    try:
        payload = service.discover_abilities(site_id)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return DiscoverAbilitiesResponse.model_validate(payload)


@router.get("/ability-info", response_model=AbilityInfoResponse)
def get_site_ability_info(
    site_id: int,
    ability_name: str,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> AbilityInfoResponse:
    service = SiteMcpProxyService(db=db, cipher=cipher)
    try:
        payload = service.get_ability_info(site_id, ability_name)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return AbilityInfoResponse.model_validate(payload)


@router.post("/abilities/execute", response_model=ExecuteAbilityResponse)
def execute_site_ability(
    site_id: int,
    body: ExecuteAbilityRequest,
    db: Annotated[Session, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> ExecuteAbilityResponse:
    service = SiteMcpProxyService(db=db, cipher=cipher)
    try:
        payload = service.execute_readonly_ability(site_id, body.ability_name, body.input)
    except SiteMcpProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return ExecuteAbilityResponse.model_validate(payload)
