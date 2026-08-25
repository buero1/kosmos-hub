from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class SiteUpdateSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    captured_at: datetime
    core_updates_json: list[dict[str, Any]]
    plugin_updates_json: list[dict[str, Any]]
    theme_updates_json: list[dict[str, Any]]
    summary_json: dict[str, Any]


class SiteUpdateRefreshResponse(BaseModel):
    site_id: int
    refreshed_at: datetime
    snapshot: SiteUpdateSnapshotResponse
