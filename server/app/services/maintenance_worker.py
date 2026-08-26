from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from threading import Lock, Thread

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.services.fleet_refresh_settings import FleetRefreshSettingsService
from app.services.maintenance_runs import MaintenanceRunService


_direct_update_poll_lock = Lock()
logger = logging.getLogger(__name__)


def process_pending_direct_updates() -> dict[str, int]:
    """Process bounded parallel updates while keeping each customer site serial."""
    empty_result = {"checked": 0, "succeeded": 0, "failed": 0, "waiting": 0, "skipped": 0}
    if not _direct_update_poll_lock.acquire(blocking=False):
        return empty_result

    try:
        summary = dict(empty_result)
        while True:
            with SessionLocal() as db:
                max_workers = FleetRefreshSettingsService(db=db).get_runtime_settings().max_parallel_direct_updates
                service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
                run_ids = service.next_parallel_direct_update_run_ids(limit=max_workers)
            if not run_ids:
                return summary

            with ThreadPoolExecutor(max_workers=len(run_ids), thread_name_prefix="kosmos-direct-update") as executor:
                futures = [executor.submit(_process_direct_update_run, run_id) for run_id in run_ids]
                for future in as_completed(futures):
                    summary["checked"] += 1
                    try:
                        outcome = future.result()
                    except Exception:
                        outcome = "failed"
                    if outcome not in summary:
                        outcome = "failed"
                    summary[outcome] += 1
    finally:
        _direct_update_poll_lock.release()


def _process_direct_update_run(run_id: int) -> str:
    """Use an isolated session because each worker commits independent evidence."""
    try:
        with SessionLocal() as db:
            service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
            return service.poll_direct_update_run(run_id)
    except Exception:
        logger.exception("Direct update worker failed for maintenance run %s.", run_id)
        try:
            with SessionLocal() as db:
                service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
                service.fail_direct_update_worker_run(run_id)
        except Exception:
            logger.exception("Could not persist the direct update worker failure for maintenance run %s.", run_id)
        return "failed"


def schedule_pending_direct_updates() -> None:
    """Start processing without holding the originating web request open."""
    Thread(
        target=process_pending_direct_updates,
        name="kosmos-direct-update-worker",
        daemon=True,
    ).start()
