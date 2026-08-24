from pydantic import BaseModel


class DashboardSummary(BaseModel):
    total_sites: int
    pending_sites: int
    verified_sites: int
    unknown_sites: int

