from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class RegistrationRequest(BaseModel):
    site_uuid: str = Field(min_length=36, max_length=36)
    site_secret: str | None = Field(default=None, min_length=32)
    home_url: HttpUrl
    site_url: HttpUrl
    wordpress_version: str
    php_version: str
    bridge_version: str
    mcp_endpoint: HttpUrl | None = None
    registration_timestamp: datetime
    heartbeat: bool = False

    @field_validator("registration_timestamp")
    @classmethod
    def ensure_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


@dataclass
class RegistrationHeaders:
    site_uuid: str | None
    timestamp: str | None
    nonce: str | None
    body_sha256: str | None
    signature: str | None
    request_id: str | None

    @classmethod
    def from_request(cls, headers) -> "RegistrationHeaders":
        return cls(
            site_uuid=headers.get("x-kosmos-site-uuid"),
            timestamp=headers.get("x-kosmos-timestamp"),
            nonce=headers.get("x-kosmos-nonce"),
            body_sha256=headers.get("x-kosmos-body-sha256"),
            signature=headers.get("x-kosmos-signature"),
            request_id=headers.get("x-request-id"),
        )


class RegistrationResponse(BaseModel):
    site_id: int
    site_uuid: str
    status: str
    message: str


class RegistrationResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    site_id: int
    site_uuid: str
    status: str
    message: str
