from threading import Lock

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.services.maintenance_runs import MaintenanceRunService


_direct_update_poll_lock = Lock()


def process_pending_direct_updates() -> dict[str, int]:
    """Process one pass of queued direct updates without concurrent execution."""
    empty_result = {"checked": 0, "succeeded": 0, "failed": 0, "waiting": 0, "skipped": 0}
    if not _direct_update_poll_lock.acquire(blocking=False):
        return empty_result

    try:
        with SessionLocal() as db:
            service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
            return service.poll_active_plugin_updates(limit=25)
    finally:
        _direct_update_poll_lock.release()
