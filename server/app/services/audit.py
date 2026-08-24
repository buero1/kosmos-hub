from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.site import Site


def write_audit_log(
    db: Session,
    *,
    site: Site | None,
    actor: str,
    source: str,
    action: str,
    result: str,
    detail: str | None = None,
    request_id: str | None = None,
) -> None:
    entry = AuditLog(
        site=site,
        actor=actor,
        source=source,
        action=action,
        result=result,
        detail=detail,
        request_id=request_id,
        timestamp=datetime.now(UTC),
    )
    db.add(entry)

