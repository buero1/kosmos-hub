from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class SiteBackupSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    captured_at: datetime
    provider: str
    provider_installed: bool
    provider_active: bool
    backup_available: bool
    backup_complete: bool
    backup_at: datetime | None
    backup_count: int
    components_json: list[str]
    summary_json: dict[str, Any]


class SiteBackupRefreshResponse(BaseModel):
    site_id: int
    refreshed_at: datetime
    snapshot: SiteBackupSnapshotResponse
