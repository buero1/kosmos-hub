from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from select import select as select_socket
from time import monotonic, sleep
from threading import Lock, Thread

from sqlalchemy import select

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.models.hub_mailbox_account import HubMailboxAccount
from app.services.customer_activities import CustomerActivityService
from app.services.fleet_refresh_settings import FleetRefreshSettingsService
from app.services.maintenance_runs import MaintenanceRunService
from app.services.user_deletion_batches import UserDeletionBatchService
from app.core.config import get_settings
from app.services.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhookService
from app.services.zoho_email_content_import import ZohoEmailContentImportService
from app.services.zoho_email_attachment_import import ZohoEmailAttachmentImportService
from app.services.zoho_email_history_import import ZohoEmailHistoryImportService
from app.services.zoho_note_history_import import ZohoNoteHistoryImportService
from app.services.zoho_books_invoice_import import ZohoBooksInvoiceImportService
from app.services.zoho_books_recurring_invoice_import import ZohoBooksRecurringInvoiceImportService
from app.services.hub_mailbox_imap_import import HubMailboxImapImportError, HubMailboxImapImportService
from app.services.hub_mailbox_health import HubMailboxHealthService
from app.services.hub_mailbox_imap_sync import HubMailboxImapSyncService
from app.services.site_inventory import SiteInventoryService
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_updates import SiteUpdateService


_direct_update_poll_lock = Lock()
_complete_site_update_poll_lock = Lock()
_user_deletion_poll_lock = Lock()
_zoho_email_workflow_poll_lock = Lock()
_zoho_email_content_import_poll_lock = Lock()
_zoho_email_attachment_import_poll_lock = Lock()
_zoho_email_history_import_poll_lock = Lock()
_zoho_note_history_import_poll_lock = Lock()
_zoho_books_invoice_import_poll_lock = Lock()
_zoho_books_recurring_invoice_import_poll_lock = Lock()
_hub_mailbox_imap_import_poll_lock = Lock()
_hub_mailbox_imap_sync_poll_lock = Lock()
_hub_mailbox_imap_inbox_sync_lock = Lock()
_hub_mailbox_imap_sync_scheduler_lock = Lock()
_hub_mailbox_imap_idle_scheduler_lock = Lock()
_hub_mailbox_imap_idle_workers_lock = Lock()
_hub_mailbox_imap_idle_workers: dict[int, Thread] = {}
_elapsed_meeting_scheduler_lock = Lock()
logger = logging.getLogger(__name__)

_INBOX_IDLE_MAX_SECONDS = 25 * 60
_INBOX_IDLE_RECONNECT_SECONDS = 5
_INBOX_IDLE_DISCOVERY_SECONDS = 60
_ELAPSED_MEETING_CHECK_SECONDS = 60


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
                service.recover_pending_post_update_diagnostics(limit=max_workers)
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


def process_elapsed_customer_meetings() -> int:
    """Persist completed status for meetings whose scheduled end has passed."""
    with SessionLocal() as db:
        return CustomerActivityService(db=db).complete_elapsed_meetings()


def schedule_elapsed_customer_meetings() -> None:
    """Check once per minute without requiring a customer or calendar page to be open."""
    if not _elapsed_meeting_scheduler_lock.acquire(blocking=False):
        return

    def complete_forever() -> None:
        try:
            while True:
                try:
                    process_elapsed_customer_meetings()
                except Exception:
                    logger.exception("Automatic customer meeting completion failed.")
                sleep(_ELAPSED_MEETING_CHECK_SECONDS)
        finally:
            _elapsed_meeting_scheduler_lock.release()

    Thread(
        target=complete_forever,
        name="kosmos-elapsed-meeting-completer",
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


def process_pending_zoho_email_attachment_import() -> dict[str, int]:
    """Copy Zoho attachment binaries serially without blocking interactive CRM work."""
    summary = {"checked": 0, "succeeded": 0, "failed": 0}
    if not _zoho_email_attachment_import_poll_lock.acquire(blocking=False):
        return summary

    try:
        while True:
            try:
                with SessionLocal() as db:
                    outcome = ZohoEmailAttachmentImportService(
                        db=db,
                        cipher=get_secret_cipher(),
                        public_base_url=get_settings().public_base_url,
                    ).process_next_attachment()
            except Exception:
                logger.exception("Zoho email attachment import worker failed unexpectedly.")
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
        _zoho_email_attachment_import_poll_lock.release()


def schedule_pending_zoho_email_attachment_import() -> None:
    """Resume an active attachment import after startup or a user request."""
    Thread(
        target=process_pending_zoho_email_attachment_import,
        name="kosmos-zoho-email-attachment-import-worker",
        daemon=True,
    ).start()


def process_pending_zoho_books_invoice_import() -> dict[str, int]:
    """Import one Books invoice at a time without delaying interactive Hub work."""
    summary = {"checked": 0, "succeeded": 0, "failed": 0}
    if not _zoho_books_invoice_import_poll_lock.acquire(blocking=False):
        return summary

    try:
        while True:
            try:
                with SessionLocal() as db:
                    outcome = ZohoBooksInvoiceImportService(
                        db=db,
                        cipher=get_secret_cipher(),
                    ).process_next_invoice()
            except Exception:
                logger.exception("Zoho Books invoice import worker failed unexpectedly.")
                summary["failed"] += 1
                return summary

            if outcome is None or outcome in {"completed", "cancelled", "stopped"}:
                return summary
            summary["checked"] += 1
            summary["succeeded" if outcome == "succeeded" else "failed"] += 1
            # Respect the remote API and leave capacity for direct Books actions.
            sleep(0.5)
    finally:
        _zoho_books_invoice_import_poll_lock.release()


def schedule_pending_zoho_books_invoice_import() -> None:
    """Resume an active Books invoice import after startup or a user request."""
    Thread(
        target=process_pending_zoho_books_invoice_import,
        name="kosmos-zoho-books-invoice-import-worker",
        daemon=True,
    ).start()


def process_pending_zoho_books_recurring_invoice_import() -> dict[str, int]:
    """Import one recurring invoice at a time without delaying interactive Hub work."""
    summary = {"checked": 0, "succeeded": 0, "failed": 0}
    if not _zoho_books_recurring_invoice_import_poll_lock.acquire(blocking=False):
        return summary

    try:
        while True:
            try:
                with SessionLocal() as db:
                    outcome = ZohoBooksRecurringInvoiceImportService(
                        db=db,
                        cipher=get_secret_cipher(),
                    ).process_next_invoice()
            except Exception:
                logger.exception("Zoho Books recurring invoice import worker failed unexpectedly.")
                summary["failed"] += 1
                return summary

            if outcome is None or outcome in {"completed", "cancelled", "stopped"}:
                return summary
            summary["checked"] += 1
            summary["succeeded" if outcome == "succeeded" else "failed"] += 1
            sleep(0.5)
    finally:
        _zoho_books_recurring_invoice_import_poll_lock.release()


def schedule_pending_zoho_books_recurring_invoice_import() -> None:
    """Resume an active Books recurring invoice import after startup or a user request."""
    Thread(
        target=process_pending_zoho_books_recurring_invoice_import,
        name="kosmos-zoho-books-recurring-invoice-import-worker",
        daemon=True,
    ).start()


def process_registered_site_refresh(site_id: int) -> None:
    """Collect initial inventory and update data after a new Bridge connects."""
    with SessionLocal() as db:
        cipher = get_secret_cipher()
        try:
            SiteInventoryService(db=db, cipher=cipher).refresh_site_state(site_id)
        except SiteMcpProxyError as exc:
            logger.info("Initial inventory refresh for site %s could not complete: %s", site_id, exc.message)
            return
        try:
            SiteUpdateService(db=db, cipher=cipher).refresh_site_updates(site_id)
        except SiteMcpProxyError as exc:
            logger.info("Initial update refresh for site %s could not complete: %s", site_id, exc.message)


def schedule_registered_site_refresh(site_id: int) -> None:
    """Do not delay the signed WordPress registration while the Hub reads site data."""
    Thread(
        target=process_registered_site_refresh,
        args=(site_id,),
        name=f"kosmos-registered-site-refresh-{site_id}",
        daemon=True,
    ).start()


def process_pending_hub_mailbox_imap_import() -> dict[str, int]:
    """Fetch one selected Mittwald message at a time without delaying web requests."""
    summary = {"checked": 0, "succeeded": 0, "skipped": 0, "failed": 0}
    if not _hub_mailbox_imap_import_poll_lock.acquire(blocking=False):
        return summary

    try:
        while True:
            try:
                with SessionLocal() as db:
                    outcome = HubMailboxImapImportService(
                        db=db,
                        cipher=get_secret_cipher(),
                        public_base_url=get_settings().public_base_url,
                    ).process_next_message()
            except Exception:
                logger.exception("Mittwald IMAP import worker failed unexpectedly.")
                summary["failed"] += 1
                return summary

            if outcome is None or outcome in {"completed", "cancelled", "stopped"}:
                return summary
            summary["checked"] += 1
            if outcome in {"succeeded", "skipped", "failed"}:
                summary["succeeded" if outcome == "succeeded" else outcome] += 1
            else:
                summary["failed"] += 1
            # Do not monopolize the process while the user works in the Hub.
            sleep(0.2)
    finally:
        _hub_mailbox_imap_import_poll_lock.release()


def schedule_pending_hub_mailbox_imap_import() -> None:
    """Resume a selected Mittwald import after startup or a user request."""
    Thread(
        target=process_pending_hub_mailbox_imap_import,
        name="kosmos-mittwald-imap-import-worker",
        daemon=True,
    ).start()


def process_hub_mailbox_imap_sync() -> dict[str, int]:
    """Run the durable one-minute fallback import for INBOX and INBOX.Sent."""
    empty_summary = {"checked": 0, "imported": 0, "skipped": 0, "failed": 0}
    if not _hub_mailbox_imap_sync_poll_lock.acquire(blocking=False):
        return empty_summary
    try:
        if not _hub_mailbox_imap_inbox_sync_lock.acquire(blocking=False):
            return empty_summary
        try:
            with SessionLocal() as db:
                summary = HubMailboxImapSyncService(
                    db=db,
                    cipher=get_secret_cipher(),
                    public_base_url=get_settings().public_base_url,
                ).sync_once()
                HubMailboxHealthService(
                    db=db,
                    cipher=get_secret_cipher(),
                    public_base_url=get_settings().public_base_url,
                ).notify_unhealthy_inboxes()
        finally:
            _hub_mailbox_imap_inbox_sync_lock.release()
        return {
            "checked": summary.checked,
            "imported": summary.imported,
            "skipped": summary.skipped,
            "failed": summary.failed,
        }
    except Exception:
        logger.exception("Mittwald IMAP sync failed unexpectedly.")
        return {**empty_summary, "failed": 1}
    finally:
        _hub_mailbox_imap_sync_poll_lock.release()


def schedule_hub_mailbox_imap_sync_polling() -> None:
    """Poll both IMAP folders once a minute as a durable fallback for INBOX IDLE."""
    if not _hub_mailbox_imap_sync_scheduler_lock.acquire(blocking=False):
        return

    def poll_forever() -> None:
        try:
            while True:
                process_hub_mailbox_imap_sync()
                sleep(60)
        finally:
            _hub_mailbox_imap_sync_scheduler_lock.release()

    Thread(
        target=poll_forever,
        name="kosmos-mittwald-imap-fallback-poller",
        daemon=True,
    ).start()


def schedule_hub_mailbox_imap_inbox_idle() -> None:
    """Maintain one IMAP IDLE connection per enabled mailbox for immediate INBOX imports."""
    if not _hub_mailbox_imap_idle_scheduler_lock.acquire(blocking=False):
        return

    def supervise_workers() -> None:
        try:
            while True:
                for mailbox_account_id in _enabled_hub_mailbox_account_ids():
                    _ensure_hub_mailbox_inbox_idle_worker(mailbox_account_id)
                sleep(_INBOX_IDLE_DISCOVERY_SECONDS)
        finally:
            _hub_mailbox_imap_idle_scheduler_lock.release()

    Thread(
        target=supervise_workers,
        name="kosmos-mittwald-imap-idle-supervisor",
        daemon=True,
    ).start()


def _enabled_hub_mailbox_account_ids() -> tuple[int, ...]:
    with SessionLocal() as db:
        return tuple(
            db.scalars(
                select(HubMailboxAccount.id)
                .where(HubMailboxAccount.enabled.is_(True), HubMailboxAccount.verified_at.is_not(None))
                .order_by(HubMailboxAccount.id.asc())
            )
        )


def _ensure_hub_mailbox_inbox_idle_worker(mailbox_account_id: int) -> None:
    with _hub_mailbox_imap_idle_workers_lock:
        worker = _hub_mailbox_imap_idle_workers.get(mailbox_account_id)
        if worker is not None and worker.is_alive():
            return
        worker = Thread(
            target=_run_hub_mailbox_inbox_idle,
            args=(mailbox_account_id,),
            name=f"kosmos-mittwald-imap-idle-{mailbox_account_id}",
            daemon=True,
        )
        _hub_mailbox_imap_idle_workers[mailbox_account_id] = worker
        worker.start()


def _run_hub_mailbox_inbox_idle(mailbox_account_id: int) -> None:
    """Reconnect and resume from the stored UID whenever an IMAP IDLE session ends."""
    try:
        while True:
            account = _idle_account(mailbox_account_id)
            if account is None:
                return
            try:
                # The UID pass catches mail delivered while this worker reconnects.
                _process_hub_mailbox_imap_inbox_sync(mailbox_account_id)
                with _idle_importer(account)._connected_imap(account) as imap:
                    status, _data = imap.select("INBOX", readonly=True)
                    if status != "OK":
                        raise HubMailboxImapImportError("Der IMAP-Ordner INBOX konnte nicht für IDLE geöffnet werden.")
                    _wait_for_hub_mailbox_idle_change(imap, duration_seconds=_INBOX_IDLE_MAX_SECONDS)
            except (HubMailboxImapImportError, OSError, ValueError):
                logger.warning("Mittwald IMAP IDLE for mailbox %s ended; reconnecting.", mailbox_account_id, exc_info=True)
                sleep(_INBOX_IDLE_RECONNECT_SECONDS)
    finally:
        with _hub_mailbox_imap_idle_workers_lock:
            _hub_mailbox_imap_idle_workers.pop(mailbox_account_id, None)


def _idle_account(mailbox_account_id: int) -> HubMailboxAccount | None:
    with SessionLocal() as db:
        account = db.get(HubMailboxAccount, mailbox_account_id)
        if account is None or not account.enabled or account.verified_at is None:
            return None
        db.expunge(account)
        return account


def _idle_importer(account: HubMailboxAccount) -> HubMailboxImapImportService:
    # The importer only uses its session for mailbox persistence, not while the IDLE socket is open.
    return HubMailboxImapImportService(
        db=SessionLocal(),
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )


def _process_hub_mailbox_imap_inbox_sync(mailbox_account_id: int) -> dict[str, int]:
    empty_summary = {"checked": 0, "imported": 0, "skipped": 0, "failed": 0}
    if not _hub_mailbox_imap_inbox_sync_lock.acquire(blocking=False):
        return empty_summary
    try:
        with SessionLocal() as db:
            summary = HubMailboxImapSyncService(
                db=db,
                cipher=get_secret_cipher(),
                public_base_url=get_settings().public_base_url,
            ).sync_once(folders=("INBOX",), mailbox_account_ids=(mailbox_account_id,))
        return {
            "checked": summary.checked,
            "imported": summary.imported,
            "skipped": summary.skipped,
            "failed": summary.failed,
        }
    except Exception:
        logger.exception("Mittwald INBOX IDLE follow-up sync failed for mailbox %s.", mailbox_account_id)
        return {**empty_summary, "failed": 1}
    finally:
        _hub_mailbox_imap_inbox_sync_lock.release()


def _wait_for_hub_mailbox_idle_change(imap, *, duration_seconds: int) -> bool:
    """Wait for an untagged IMAP response, then end IDLE cleanly before importing."""
    capabilities = {str(capability).upper() for capability in imap.capabilities}
    if "IDLE" not in capabilities:
        raise HubMailboxImapImportError("Der Mittwald-Server unterstützt IMAP IDLE für dieses Postfach nicht.")

    tag = imap._new_tag()
    imap.send(tag + b" IDLE\r\n")
    continuation = _read_hub_mailbox_idle_line(imap, deadline=monotonic() + 10)
    if not continuation.startswith(b"+"):
        raise HubMailboxImapImportError("Der Mittwald-Server hat die IMAP-IDLE-Anfrage abgelehnt.")

    deadline = monotonic() + duration_seconds
    changed = False
    idle_active = True
    try:
        while monotonic() < deadline:
            try:
                response = _read_hub_mailbox_idle_line(imap, deadline=deadline)
            except TimeoutError:
                break
            if response.startswith(b"*"):
                changed = True
                break
            if response.startswith(tag):
                idle_active = False
                if b"OK" not in response.upper():
                    raise HubMailboxImapImportError("Die IMAP-IDLE-Verbindung wurde vom Mittwald-Server beendet.")
                return changed
    finally:
        if idle_active:
            imap.send(b"DONE\r\n")
            _finish_hub_mailbox_idle(imap, tag=tag)
    return changed


def _read_hub_mailbox_idle_line(imap, *, deadline: float) -> bytes:
    timeout = deadline - monotonic()
    if timeout <= 0:
        raise TimeoutError("IMAP IDLE deadline reached")
    ready, _write_ready, _errors = select_socket((imap.sock,), (), (), timeout)
    if not ready:
        raise TimeoutError("IMAP IDLE deadline reached")
    response = imap.readline()
    if not response:
        raise HubMailboxImapImportError("Die IMAP-IDLE-Verbindung wurde geschlossen.")
    return response


def _finish_hub_mailbox_idle(imap, *, tag: bytes) -> None:
    deadline = monotonic() + 10
    while True:
        try:
            response = _read_hub_mailbox_idle_line(imap, deadline=deadline)
        except TimeoutError as exc:
            raise HubMailboxImapImportError("Der Mittwald-Server hat IMAP IDLE nicht sauber beendet.") from exc
        if not response.startswith(tag):
            continue
        if b"OK" not in response.upper():
            raise HubMailboxImapImportError("Der Mittwald-Server hat IMAP IDLE mit einem Fehler beendet.")
        return
