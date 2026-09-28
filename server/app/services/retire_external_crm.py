"""Idempotently retire connection credentials and unfinished import jobs locally."""

from datetime import UTC, datetime

from sqlalchemy import or_, update

from app.db.base import Base
from app.models.zoho_connection import ZohoConnection
from app.models.zoho_books_connection import ZohoBooksConnection
from app.models.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhook
from app.models.customer_communication import CustomerZohoNote


def retire_external_crm(db) -> None:
    for model in (ZohoConnection, ZohoBooksConnection):
        db.execute(
            update(model)
            .where(
                or_(
                    model.encrypted_client_id != "",
                    model.encrypted_client_secret != "",
                    model.encrypted_refresh_token.is_not(None),
                )
            )
            .values(
                encrypted_client_id="",
                encrypted_client_secret="",
                encrypted_refresh_token=None,
                connected_at=None,
                last_error="Zoho-Anbindung stillgelegt; Daten werden nur im Hub verwaltet.",
            )
        )
    db.execute(
        update(ZohoEmailWorkflowWebhook)
        .where(ZohoEmailWorkflowWebhook.encrypted_token != "")
        .values(encrypted_token="")
    )
    db.execute(
        update(CustomerZohoNote)
        .where(
            CustomerZohoNote.source == "hub",
            CustomerZohoNote.sync_status.in_(("pending", "failed")),
        )
        .values(sync_status="local", last_error=None)
    )
    for table in Base.metadata.tables.values():
        if not table.name.startswith("zoho_") or "status" not in table.c:
            continue
        values = {"status": "cancelled"}
        if "last_error" in table.c:
            values["last_error"] = "Zoho-Synchronisierung stillgelegt."
        if "completed_at" in table.c:
            values["completed_at"] = datetime.now(UTC)
        db.execute(
            update(table)
            .where(table.c.status.in_(("pending", "running", "queued")))
            .values(**values)
        )
