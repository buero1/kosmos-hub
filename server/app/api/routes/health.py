from datetime import UTC, datetime

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}

