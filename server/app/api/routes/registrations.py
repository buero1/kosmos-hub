import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import SecretCipher, get_secret_cipher
from app.db.session import get_db
from app.schemas.registration import RegistrationHeaders, RegistrationRequest, RegistrationResponse
from app.services.site_registration import SiteRegistrationService

router = APIRouter(prefix="/api/v1", tags=["registrations"])


@router.post("/registrations", response_model=RegistrationResponse, status_code=status.HTTP_200_OK)
async def register_site(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    cipher: Annotated[SecretCipher, Depends(get_secret_cipher)],
) -> RegistrationResponse:
    raw_body = await request.body()
    try:
        payload = RegistrationRequest.model_validate(json.loads(raw_body))
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    headers = RegistrationHeaders.from_request(request.headers)
    service = SiteRegistrationService(db=db, settings=settings, cipher=cipher)
    return service.register(payload=payload, headers=headers, raw_body=raw_body)
