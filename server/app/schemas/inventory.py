from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class StoredSiteCapabilityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    capability: str
    provider: str
    ability_name: str
    ability_schema: dict[str, Any]
    read_only: bool
    destructive: bool
    last_discovered_at: datetime


class SiteCapabilityInventoryResponse(BaseModel):
    items: list[StoredSiteCapabilityResponse]


class SiteInventoryRefreshResponse(BaseModel):
    site_id: int
    provider: str
    refreshed_at: datetime
    items: list[StoredSiteCapabilityResponse]


class SiteStateSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    captured_at: datetime
    wordpress_version: str | None
    php_version: str | None
    plugins_json: list[dict[str, Any]]
    themes_json: list[dict[str, Any]]
    environment_json: dict[str, Any]


class SiteStateRefreshResponse(BaseModel):
    site_id: int
    refreshed_at: datetime
    snapshot: SiteStateSnapshotResponse
