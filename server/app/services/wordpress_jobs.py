"""Durable dispatch boundary for confirmed, non-transactional WordPress actions."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging

from sqlalchemy import select, update

from app.models.hub_wordpress_job import HubWordPressJob
from app.models.maintenance_run import MaintenanceRun
from app.models.user_deletion_batch import UserDeletionBatch
from app.services.hub_operations import HubOperationError, HubOperationResult, HubOperationService
from app.services.hub_operation_websites import website_site
from app.services.hub_record_access import identifier, require_actor
from app.services.wordpress_remote_catalog import prepare_remote, execute_remote
from app.services.plugin_auto_updates import PluginAutoUpdateResult


def enqueue(service, values, *, key):
    _spec, _kwargs, site_ids = prepare_remote(service, key, values)
    job = HubWordPressJob(operation_key=key, actor=service.actor, site_ids=site_ids,
        encrypted_input=service.cipher.encrypt(json.dumps(dict(values))), status="queued", result_json={})
    service.db.add(job)
    service.db.flush()
    return HubOperationResult("WordPress-Auftrag ansehen", f"/wordpress/jobs/{job.id}", job.id,
        outputs={"job_id": str(job.id), "status": "queued"})


def require_job(service, values, *, lock=False):
    user, _access = require_actor(service, "websites", "view")
    job_id = identifier(values.get("job_id", ""))
    statement = select(HubWordPressJob).where(HubWordPressJob.id == job_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    job = service.db.scalar(statement)
    if job is None or (job.actor != user.username and user.role != "admin"):
        raise HubOperationError("Dieser WordPress-Auftrag ist nicht verfuegbar.")
    for site_id in job.site_ids:
        website_site(service, site_id)
    return job


def job_status(service, values):
    job = require_job(service, values)
    runs = []
    for run_id in job.result_json.get("run_ids", []):
        run = service.db.get(MaintenanceRun, run_id)
        if run is not None and run.site_id in job.site_ids:
            runs.append({"run_id": str(run.id), "site_id": str(run.site_id), "status": run.status,
                "kind": run.kind, "href": f"/sites/{run.site_id}"})
    deletion = None
    if job.result_json.get("deletion_batch_id"):
        from app.services.wordpress_readers import deletion_batch
        batch = deletion_batch(service, int(job.result_json["deletion_batch_id"]))
        deletion = {"batch_id": str(batch.id), "status": batch.status, "href": f"/users?deletion_batch_id={batch.id}"}
    return {"job_id": str(job.id), "operation": job.operation_key, "status": job.status, "message": job.message, "deletion": deletion,
        "site_ids": job.site_ids, "runs": runs, "result": job.result_json,
        "can_cancel": job.status == "queued"}


def cancel_job(service, values):
    require_actor(service, "websites", "edit")
    job = require_job(service, values, lock=True)
    if job.status != "queued":
        raise HubOperationError("Der Auftrag wurde bereits uebergeben. Laufende Wartung ueber deren Abbruchfunktion stoppen.")
    job.status, job.encrypted_input = "cancelled", None
    job.message, job.finished_at = "Vor Ausfuehrung abgebrochen.", datetime.now(UTC)
    service.db.flush()
    return HubOperationResult("WordPress-Auftrag", f"/wordpress/jobs/{job.id}", job.id, outputs={"job_id": str(job.id), "status": job.status})


def safe_outcome(result, service=None):
    if isinstance(result, PluginAutoUpdateResult):
        return result.outcome()
    if isinstance(result, UserDeletionBatch):
        return {"deletion_batch_id": str(result.id), "batch_status": result.status}
    run = getattr(result, "run", None)
    runs = getattr(result, "runs", None)
    if run is not None:
        return {"run_ids": [run.id], "outcome": getattr(result, "result", "submitted")}
    if runs is not None:
        return {"run_ids": [item.id for item in runs], "batch_id": str(getattr(result, "batch_id", ""))}
    if hasattr(result, "batch_id") and hasattr(result, "run_count"):
        from app.services.maintenance_runs import MaintenanceRunService
        maintenance = MaintenanceRunService(db=service.db, cipher=service.cipher)
        runs = maintenance.list_plugin_update_batch(result.batch_id) or maintenance.list_plugin_installation_batch(result.batch_id)
        return {"run_ids": [item.id for item in runs], "batch_id": result.batch_id}
    if isinstance(result, list):
        return {"outcomes": [{"site_id": item.get("site_id"), "status": item.get("status")} for item in result if isinstance(item, dict)]}
    if isinstance(result, dict):
        return {key: result[key] for key in ("id", "username", "deleted") if key in result}
    return {}


def process_next(session_factory, cipher):
    with session_factory() as db:
        # A crashed dispatcher must never repeat a potentially completed remote call.
        db.execute(update(HubWordPressJob).where(HubWordPressJob.status == "running",
            HubWordPressJob.started_at < datetime.now(UTC) - timedelta(hours=1)).values(
            status="uncertain", encrypted_input=None, message="Ausfuehrung unterbrochen. Website/Wartungsprotokoll pruefen; keine automatische Wiederholung.", finished_at=datetime.now(UTC)))
        db.commit()
        job_id = db.scalar(select(HubWordPressJob.id).where(HubWordPressJob.status == "queued").order_by(HubWordPressJob.id).limit(1))
        if job_id is None:
            return False
        claimed = db.execute(update(HubWordPressJob).where(HubWordPressJob.id == job_id, HubWordPressJob.status == "queued")
            .values(status="running", started_at=datetime.now(UTC), message="Wird ausgefuehrt."))
        db.commit()
        if claimed.rowcount != 1:
            return True
        job = db.get(HubWordPressJob, job_id)
        service = HubOperationService(db=db, cipher=cipher, actor=job.actor)
        try:
            values = json.loads(cipher.decrypt(job.encrypted_input))
            prepare_remote(service, job.operation_key, values)  # Rights may have changed since confirmation.
        except Exception:
            db.rollback()
            job = db.get(HubWordPressJob, job_id)
            job.status, job.message = "failed", "Auftrag vor Fernzugriff abgewiesen. Eingaben und aktuelle Berechtigungen pruefen."
        else:
            job.encrypted_input = None
            db.commit()
            try:
                db.info["wordpress_job_id"] = job_id
                result = execute_remote(service, job.operation_key, values)
                outcome = safe_outcome(result, service)
                job = db.get(HubWordPressJob, job_id)
                job.result_json = outcome
                if outcome.get("outcome") in {"blocked", "error", "failed"}:
                    job.status, job.message = "failed", "Der Fachdienst hat den Auftrag abgewiesen. Wartungsprotokoll pruefen."
                elif outcome.get("run_ids") or outcome.get("batch_id") or outcome.get("batch_status") in {"queued", "running"}:
                    job.status, job.message = "submitted", "An Wartung uebergeben. Das ist noch kein Abschluss; siehe Status der einzelnen Laeufe."
                elif any(item.get("status") != "succeeded" for item in outcome.get("outcomes", [])):
                    job.status, job.message = "failed", "Nicht alle Teilaktionen waren erfolgreich. Einzelstatus und Website pruefen."
                else:
                    job.status, job.message = "succeeded", "Fachfunktion erfolgreich ausgefuehrt."
            except Exception:
                db.rollback()
                job = db.get(HubWordPressJob, job_id)
                job.status, job.message = "uncertain", "Ergebnis nicht sicher bestaetigt. Website/Wartungsprotokoll pruefen; keine automatische Wiederholung."
        job.encrypted_input, job.finished_at = None, datetime.now(UTC)
        db.commit()
        return True


async def run_wordpress_worker():
    from app.core.security import get_secret_cipher
    from app.db.session import SessionLocal
    while True:
        try:
            processed = await asyncio.to_thread(process_next, SessionLocal, get_secret_cipher())
        except Exception:
            logging.getLogger(__name__).error("WordPress dispatcher failed; queued jobs remain durable.")
            processed = False
        await asyncio.sleep(0.1 if processed else 5)
