from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SiteConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    endpoint: str
    auth_type: str
    status: str
    last_success_at: datetime | None
    last_error_at: datetime | None


class SiteDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uuid: str
    customer_id: int | None
    domain: str
    home_url: str
    site_url: str
    status: str
    wordpress_version: str | None
    php_version: str | None
    bridge_version: str | None
    last_seen_at: datetime | None
    registered_at: datetime | None
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime
    connections: list[SiteConnectionResponse] = []


class SiteListResponse(BaseModel):
    items: list[SiteDetailResponse]

