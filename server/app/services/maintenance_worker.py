from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from time import sleep
from threading import Lock, Thread

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.services.fleet_refresh_settings import FleetRefreshSettingsService
from app.services.maintenance_runs import MaintenanceRunService
from app.services.user_deletion_batches import UserDeletionBatchService
from app.core.config import get_settings
from app.services.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhookService
from app.services.zoho_email_content_import import ZohoEmailContentImportService
from app.services.zoho_email_history_import import ZohoEmailHistoryImportService
from app.services.zoho_note_history_import import ZohoNoteHistoryImportService


_direct_update_poll_lock = Lock()
_complete_site_update_poll_lock = Lock()
_user_deletion_poll_lock = Lock()
_zoho_email_workflow_poll_lock = Lock()
_zoho_email_content_import_poll_lock = Lock()
_zoho_email_history_import_poll_lock = Lock()
_zoho_note_history_import_poll_lock = Lock()
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
                recovered = service.recover_stale_direct_update_postflights(limit=max_workers)
                summary["checked"] += sum(recovered.values())
                for outcome, count in recovered.items():
                    summary[outcome] += count
                run_ids = service.next_parallel_direct_update_run_ids(limit=max_workers)
                direct_update_batch_ids = service.direct_update_batch_ids_for_run_ids(run_ids)
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

            with SessionLocal() as db:
                service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
                summary["skipped"] += service.stop_direct_update_batches_after_failure_streak(direct_update_batch_ids)
    finally:
        _direct_update_poll_lock.release()


def _process_direct_update_run(run_id: int) -> str:
    """Use an isolated session because each worker commits independent evidence."""
    try:
        with SessionLocal() as db:
            service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
            return service.poll_direct_maintenance_run(run_id)
    except Exception:
        logger.exception("Direct update worker failed for maintenance run %s.", run_id)
        try:
            with SessionLocal() as db:
                service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
                service.fail_direct_maintenance_worker_run(run_id)
        except Exception:
            logger.exception("Could not persist the direct update worker failure for maintenance run %s.", run_id)
        return "failed"


def process_pending_complete_site_updates() -> dict[str, int]:
    """Run one full website workflow at a time while its component updates stay serial."""
    empty_result = {"checked": 0, "succeeded": 0, "failed": 0, "waiting": 0, "skipped": 0}
    if not _complete_site_update_poll_lock.acquire(blocking=False):
        return empty_result

    try:
        summary = dict(empty_result)
        while True:
            with SessionLocal() as db:
                service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
                run_ids = service.next_complete_site_update_run_ids(limit=1)
            if not run_ids:
                return summary

            for run_id in run_ids:
                summary["checked"] += 1
                outcome = _process_complete_site_update_run(run_id)
                if outcome not in summary:
                    outcome = "failed"
                summary[outcome] += 1
    finally:
        _complete_site_update_poll_lock.release()


def process_pending_user_deletions() -> dict[str, int]:
    """Run reviewed user deletions serially so each reassignment stays auditable."""
    empty_result = {"checked": 0, "succeeded": 0, "failed": 0, "waiting": 0, "skipped": 0}
    if not _user_deletion_poll_lock.acquire(blocking=False):
        return empty_result

    try:
        summary = dict(empty_result)
        with SessionLocal() as db:
            UserDeletionBatchService(db=db, cipher=get_secret_cipher()).recover_interrupted_batches()

        while True:
            with SessionLocal() as db:
                item_id = UserDeletionBatchService(db=db, cipher=get_secret_cipher()).claim_next_item_id()
            if item_id is None:
                return summary

            summary["checked"] += 1
            try:
                with SessionLocal() as db:
                    outcome = UserDeletionBatchService(db=db, cipher=get_secret_cipher()).process_claimed_item(item_id)
            except Exception:
                logger.exception("User deletion worker failed for batch item %s.", item_id)
                outcome = "failed"
            if outcome == "cancelled":
                summary["skipped"] += 1
            elif outcome in summary:
                summary[outcome] += 1
            else:
                summary["failed"] += 1
    finally:
        _user_deletion_poll_lock.release()


def _process_complete_site_update_run(run_id: int) -> str:
    try:
        with SessionLocal() as db:
            service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
            return service.poll_complete_site_update_run(run_id)
    except Exception:
        logger.exception("Complete site update worker failed for maintenance run %s.", run_id)
        try:
            with SessionLocal() as db:
                service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
                service.fail_complete_site_update_worker_run(run_id)
        except Exception:
            logger.exception("Could not persist complete site update worker failure for maintenance run %s.", run_id)
        return "failed"


def schedule_pending_direct_updates() -> None:
    """Start processing without holding the originating web request open."""
    Thread(
        target=process_pending_direct_updates,
        name="kosmos-direct-update-worker",
        daemon=True,
    ).start()


def schedule_pending_complete_site_updates() -> None:
    """Start the single-site complete workflow without holding the web request open."""
    Thread(
        target=process_pending_complete_site_updates,
        name="kosmos-complete-site-update-worker",
        daemon=True,
    ).start()


def schedule_pending_user_deletions() -> None:
    """Start reviewed user deletion work without holding the originating request open."""
    Thread(
        target=process_pending_user_deletions,
        name="kosmos-user-deletion-worker",
        daemon=True,
    ).start()


def process_pending_zoho_email_workflow_deliveries() -> dict[str, int]:
    """Synchronize accepted Zoho webhooks without holding Zoho's HTTP request open."""
    summary = {"checked": 0, "succeeded": 0, "failed": 0}
    if not _zoho_email_workflow_poll_lock.acquire(blocking=False):
        return summary

    try:
        while True:
            try:
                with SessionLocal() as db:
                    outcome = ZohoEmailWorkflowWebhookService(
                        db=db,
                        cipher=get_secret_cipher(),
                        public_base_url=get_settings().public_base_url,
                    ).process_next_delivery()
            except Exception:
                logger.exception("Zoho email webhook worker failed unexpectedly.")
                summary["failed"] += 1
                return summary

            if outcome is None:
                return summary
            summary["checked"] += 1
            summary["succeeded" if outcome == "succeeded" else "failed"] += 1
    finally:
        _zoho_email_workflow_poll_lock.release()


def schedule_pending_zoho_email_workflow_deliveries() -> None:
    """Start durable Zoho webhook work after acknowledging the delivery to Zoho."""
    Thread(
        target=process_pending_zoho_email_workflow_deliveries,
        name="kosmos-zoho-email-webhook-worker",
        daemon=True,
    ).start()


def process_pending_zoho_email_history_import() -> dict[str, int]:
    """Import historical email headers serially to keep Zoho API pressure predictable."""
    summary = {"checked": 0, "succeeded": 0, "failed": 0}
    if not _zoho_email_history_import_poll_lock.acquire(blocking=False):
        return summary

    try:
        while True:
            try:
                with SessionLocal() as db:
                    outcome = ZohoEmailHistoryImportService(
                        db=db,
                        cipher=get_secret_cipher(),
                        public_base_url=get_settings().public_base_url,
                    ).process_next_customer()
            except Exception:
                logger.exception("Zoho email history import worker failed unexpectedly.")
                summary["failed"] += 1
                return summary

            if outcome is None or outcome in {"completed", "cancelled", "stopped"}:
                return summary
            summary["checked"] += 1
            if outcome == "failed":
                summary["failed"] += 1
                return summary
            summary["succeeded"] += 1
            # A small delay preserves Zoho capacity for interactive Hub actions.
            sleep(0.25)
    finally:
        _zoho_email_history_import_poll_lock.release()


def schedule_pending_zoho_email_history_import() -> None:
    """Resume an active header import after startup or a user request."""
    Thread(
        target=process_pending_zoho_email_history_import,
        name="kosmos-zoho-email-history-import-worker",
        daemon=True,
    ).start()


def process_pending_zoho_note_history_import() -> dict[str, int]:
    """Import historical Account notes serially to keep Zoho API pressure predictable."""
    summary = {"checked": 0, "succeeded": 0, "failed": 0}
    if not _zoho_note_history_import_poll_lock.acquire(blocking=False):
        return summary

    try:
        while True:
            try:
                with SessionLocal() as db:
                    outcome = ZohoNoteHistoryImportService(
                        db=db,
                        cipher=get_secret_cipher(),
                        public_base_url=get_settings().public_base_url,
                    ).process_next_customer()
            except Exception:
                logger.exception("Zoho note history import worker failed unexpectedly.")
                summary["failed"] += 1
                return summary

            if outcome is None or outcome == "completed":
                return summary
            summary["checked"] += 1
            if outcome == "failed":
                summary["failed"] += 1
                return summary
            summary["succeeded"] += 1
            # A small delay preserves Zoho capacity for interactive Hub actions.
            sleep(0.25)
    finally:
        _zoho_note_history_import_poll_lock.release()


def schedule_pending_zoho_note_history_import() -> None:
    """Resume an active note import after startup or a user request."""
    Thread(
        target=process_pending_zoho_note_history_import,
        name="kosmos-zoho-note-history-import-worker",
        daemon=True,
    ).start()


def process_pending_zoho_email_content_import() -> dict[str, int]:
    """Load selected full email bodies serially and retain failures for inspection."""
    summary = {"checked": 0, "succeeded": 0, "failed": 0}
    if not _zoho_email_content_import_poll_lock.acquire(blocking=False):
        return summary

    try:
        while True:
            try:
                with SessionLocal() as db:
                    outcome = ZohoEmailContentImportService(
                        db=db,
                        cipher=get_secret_cipher(),
                        public_base_url=get_settings().public_base_url,
                    ).process_next_email()
            except Exception:
                logger.exception("Zoho email content import worker failed unexpectedly.")
                summary["failed"] += 1
                return summary

            if outcome is None or outcome in {"completed", "cancelled", "stopped"}:
                return summary
            if outcome == "continued":
                sleep(0.5)
                continue
            summary["checked"] += 1
            summary["succeeded" if outcome == "succeeded" else "failed"] += 1
            # Preserve capacity for direct customer and email actions in Zoho.
            sleep(0.5)
    finally:
        _zoho_email_content_import_poll_lock.release()


def schedule_pending_zoho_email_content_import() -> None:
    """Resume an active full-email-content import after startup or a user request."""
    Thread(
        target=process_pending_zoho_email_content_import,
        name="kosmos-zoho-email-content-import-worker",
        daemon=True,
    ).start()
