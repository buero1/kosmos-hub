import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select, text
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import accounts, agent, assistant, desktop_notifications, health, registrations, site_abilities, site_backups, site_inventory, site_updates, sites, web
from app.core.config import get_settings
from app.core.mcp_context import reset_mcp_actor, set_mcp_actor
from app.core.security import get_secret_cipher
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.fleet_refresh_run import FleetRefreshSiteResult
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerEmailAttachment, CustomerZohoEmail, CustomerZohoEmailImage, CustomerZohoNote
from app.models.customer_activity import CustomerCallActivity, CustomerCallReminder, CustomerMeetingActivity, CustomerMeetingReminder, CustomerTaskActivity
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_desktop_device import HubDesktopDevice
from app.models.hub_case import HubCase
from app.models.hub_lead import HubLead
from app.models.hub_lead_email import HubLeadEmail
from app.models.hub_lead_note import HubLeadNote
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_offer import HubFinanceOffer, HubFinanceOfferLine
from app.models.hub_agent import HubAgentAction, HubAgentConversation, HubAgentConversationContext, HubAgentJob
from app.models.hub_workflow import HubWorkflow
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.email_compose_image import EmailComposeImage
from app.models.email_composer_settings import EmailComposerSettings
from app.models.email_ai_prompt_preset import EmailAiPromptPreset
from app.models.hub_mailbox_imap_import import HubMailboxImapImport, HubMailboxImapImportItem
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.models.hub_mailbox_email import HubMailboxAttachment
from app.models.site_user_snapshot import SiteUserSnapshot
from app.models.styling_settings import StylingSettings
from app.models.module_layout import ModuleLayout
from app.models.plugin_installation_package import PluginInstallationPackage
from app.models.user_deletion_batch import UserDeletionBatch, UserDeletionBatchItem
from app.mcp_server import hub_mcp, mcp_asgi_app
from app.models.zoho_email_workflow_delivery import ZohoEmailWorkflowDelivery
from app.models.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhook
from app.models.zoho_email_content_import import ZohoEmailContentImport, ZohoEmailContentImportItem
from app.models.zoho_email_attachment_import import ZohoEmailAttachmentImport, ZohoEmailAttachmentImportItem
from app.models.zoho_email_history_import import ZohoEmailHistoryImport
from app.models.zoho_note_history_import import ZohoNoteHistoryImport
from app.models.zoho_books_connection import ZohoBooksConnection
from app.services.hub_accounts import HubAccountService
from app.services.hub_workflows import HubWorkflowService
from app.services.email_ai_prompt_presets import EmailAiPromptPresetService
from app.services.fleet_refresh import FleetRefreshService
from app.services.maintenance_runs import MaintenanceRunService
from app.services.customer_communications import CustomerCommunicationService
from app.services.maintenance_worker import (
    process_pending_complete_site_updates,
    process_pending_direct_updates,
    process_pending_user_deletions,
    schedule_pending_user_deletions,
    schedule_pending_zoho_email_content_import,
    schedule_pending_zoho_email_attachment_import,
    schedule_pending_hub_mailbox_imap_import,
    schedule_elapsed_customer_meetings,
    schedule_hub_mailbox_imap_inbox_idle,
    schedule_hub_mailbox_imap_sync_polling,
    schedule_pending_zoho_email_history_import,
    schedule_pending_zoho_note_history_import,
    schedule_pending_zoho_email_workflow_deliveries,
)
from app.services.task_email_reminder_worker import TaskEmailReminderWorker

logger = logging.getLogger(__name__)


def _ensure_phase_one_schema() -> None:
    """Apply the small additive schema changes used before Alembic is introduced."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    if "site_user_snapshots" not in table_names:
        SiteUserSnapshot.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created site_user_snapshots table.")

    if "plugin_installation_packages" not in table_names:
        PluginInstallationPackage.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created plugin_installation_packages table.")

    if "fleet_refresh_site_results" not in table_names:
        FleetRefreshSiteResult.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created fleet_refresh_site_results table.")

    if "customer_contacts" not in table_names:
        CustomerContact.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created customer_contacts table.")
    else:
        contact_columns = {column["name"]: column for column in inspector.get_columns("customer_contacts")}
        customer_id_column = contact_columns.get("customer_id")
        zoho_id_column = contact_columns.get("zoho_id")
        if customer_id_column is not None and not customer_id_column.get("nullable", False):
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE customer_contacts MODIFY COLUMN customer_id INT NULL"))
            logger.info("Made customer_contacts.customer_id optional for Hub contacts.")
        if zoho_id_column is not None and not zoho_id_column.get("nullable", False):
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE customer_contacts MODIFY COLUMN zoho_id VARCHAR(255) NULL"))
            logger.info("Made customer_contacts.zoho_id optional for Hub contacts.")

    if "hub_cases" not in table_names:
        HubCase.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_cases table.")
    else:
        case_columns = {column["name"] for column in inspector.get_columns("hub_cases")}
        case_additions = {
            "zoho_id": "VARCHAR(255) NULL",
            "zoho_modified_at": "DATETIME NULL",
            "zoho_synced_at": "DATETIME NULL",
        }
        missing_case_columns = [(name, definition) for name, definition in case_additions.items() if name not in case_columns]
        if missing_case_columns:
            with engine.begin() as connection:
                for name, definition in missing_case_columns:
                    connection.execute(text(f"ALTER TABLE hub_cases ADD COLUMN {name} {definition}"))
            logger.info("Added hub_cases columns: %s", ", ".join(name for name, _ in missing_case_columns))
        inspector = inspect(engine)
        case_indexes = {index["name"] for index in inspector.get_indexes("hub_cases")}
        if "ix_hub_cases_zoho_id" not in case_indexes:
            with engine.begin() as connection:
                connection.execute(text("CREATE UNIQUE INDEX ix_hub_cases_zoho_id ON hub_cases (zoho_id)"))
            logger.info("Added hub_cases.zoho_id index.")

    if "hub_leads" not in table_names:
        HubLead.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_leads table.")

    if "hub_lead_emails" not in table_names:
        HubLeadEmail.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_lead_emails table.")

    if "hub_lead_notes" not in table_names:
        HubLeadNote.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_lead_notes table.")

    if "hub_finance_articles" not in table_names:
        HubFinanceArticle.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_finance_articles table.")

    if "hub_finance_offers" not in table_names:
        HubFinanceOffer.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_finance_offers table.")

    if "hub_finance_offer_lines" not in table_names:
        HubFinanceOfferLine.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_finance_offer_lines table.")

    if "zoho_books_connections" not in table_names:
        ZohoBooksConnection.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_books_connections table.")

    if "hub_workflows" not in table_names:
        HubWorkflow.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_workflows table.")

    if "hub_agent_conversations" not in table_names:
        HubAgentConversation.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_agent_conversations table.")

    if "hub_agent_conversation_contexts" not in table_names:
        HubAgentConversationContext.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_agent_conversation_contexts table.")

    if "hub_agent_jobs" not in table_names:
        HubAgentJob.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_agent_jobs table.")
    else:
        agent_job_columns = {column["name"] for column in inspector.get_columns("hub_agent_jobs")}
        if "conversation_id" not in agent_job_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE hub_agent_jobs ADD COLUMN conversation_id INT NULL"))
                connection.execute(text("CREATE INDEX ix_hub_agent_jobs_conversation_id ON hub_agent_jobs (conversation_id)"))
            logger.info("Added hub_agent_jobs.conversation_id column.")

    if "hub_agent_actions" not in table_names:
        HubAgentAction.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_agent_actions table.")

    if "email_ai_prompt_presets" not in table_names:
        EmailAiPromptPreset.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created email_ai_prompt_presets table.")

    if "customer_zoho_notes" not in table_names:
        CustomerZohoNote.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created customer_zoho_notes table.")

    for activity_model in (
        CustomerCallActivity,
        CustomerCallReminder,
        CustomerTaskActivity,
        CustomerMeetingActivity,
        CustomerMeetingReminder,
        CustomerActivityReminderNotification,
        HubDesktopDevice,
    ):
        if activity_model.__tablename__ not in table_names:
            activity_model.__table__.create(bind=engine, checkfirst=True)
            logger.info("Created %s table.", activity_model.__tablename__)

    optional_activity_customer_tables = (
        "customer_call_activities",
        "customer_task_activities",
        "customer_meeting_activities",
        "customer_activity_reminder_notifications",
    )
    for table_name in optional_activity_customer_tables:
        if table_name not in table_names:
            continue
        customer_id_column = next(
            (column for column in inspector.get_columns(table_name) if column["name"] == "customer_id"),
            None,
        )
        if customer_id_column is not None and not customer_id_column.get("nullable", False):
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table_name} MODIFY COLUMN customer_id INT NULL"))
            logger.info("Made %s.customer_id optional for calendar activities.", table_name)

    if "customer_task_activities" in table_names:
        task_columns = {column["name"] for column in inspector.get_columns("customer_task_activities")}
        with engine.begin() as connection:
            if "reminder_channel" not in task_columns:
                connection.execute(
                    text("ALTER TABLE customer_task_activities ADD COLUMN reminder_channel VARCHAR(16) NULL AFTER due_at")
                )
                logger.info("Added customer_task_activities.reminder_channel column.")
            if "reminder_minutes_before" not in task_columns:
                connection.execute(
                    text(
                        "ALTER TABLE customer_task_activities "
                        "ADD COLUMN reminder_minutes_before INT NULL AFTER reminder_channel"
                    )
                )
                logger.info("Added customer_task_activities.reminder_minutes_before column.")
            if "case_id" not in task_columns:
                connection.execute(
                    text("ALTER TABLE customer_task_activities ADD COLUMN case_id INT NULL AFTER customer_id")
                )
                connection.execute(
                    text("CREATE INDEX ix_customer_task_activities_case_id ON customer_task_activities (case_id)")
                )
                logger.info("Added customer_task_activities.case_id column and index.")

    if "customer_zoho_emails" not in table_names:
        CustomerZohoEmail.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created customer_zoho_emails table.")
    else:
        columns = {column["name"] for column in inspector.get_columns("customer_zoho_emails")}
        if "is_unread" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE customer_zoho_emails ADD COLUMN is_unread TINYINT(1) NOT NULL DEFAULT 1 AFTER direction")
                )
                connection.execute(text("UPDATE customer_zoho_emails SET is_unread = 0"))
            logger.info("Added customer_zoho_emails.is_unread; existing emails were marked read.")
        if "mailbox_state" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE customer_zoho_emails "
                        "ADD COLUMN mailbox_state VARCHAR(16) NOT NULL DEFAULT 'active' AFTER is_unread"
                    )
                )
            logger.info("Added customer_zoho_emails.mailbox_state column.")
        if "encrypted_header_json" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE customer_zoho_emails "
                        "ADD COLUMN encrypted_header_json TEXT NOT NULL DEFAULT '' AFTER encrypted_payload_json"
                    )
                )
            logger.info("Added customer_zoho_emails.encrypted_header_json column.")
        unique_constraints = inspector.get_unique_constraints("customer_zoho_emails")
        legacy_unique_names = [
            constraint["name"]
            for constraint in unique_constraints
            if constraint.get("column_names") == ["zoho_message_id"] and constraint.get("name")
        ]
        has_customer_message_constraint = any(
            constraint.get("column_names") == ["customer_id", "zoho_message_id"]
            for constraint in unique_constraints
        )
        if legacy_unique_names or not has_customer_message_constraint:
            with engine.begin() as connection:
                for constraint_name in legacy_unique_names:
                    connection.execute(text(f"ALTER TABLE customer_zoho_emails DROP INDEX `{constraint_name}`"))
                if not has_customer_message_constraint:
                    connection.execute(
                        text(
                            "ALTER TABLE customer_zoho_emails "
                            "ADD CONSTRAINT uq_customer_zoho_emails_customer_id_zoho_message_id "
                            "UNIQUE (customer_id, zoho_message_id)"
                        )
                    )
            logger.info("Changed customer_zoho_emails uniqueness to customer_id plus zoho_message_id.")

    if "customer_zoho_email_images" not in table_names:
        CustomerZohoEmailImage.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created customer_zoho_email_images table.")

    if "customer_email_attachments" not in table_names:
        CustomerEmailAttachment.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created customer_email_attachments table.")

    if "customer_zoho_emails" in table_names:
        columns = {column["name"]: column for column in inspector.get_columns("customer_zoho_emails")}
        payload_column = columns.get("encrypted_payload_json")
        if payload_column is not None and "MEDIUMTEXT" not in str(payload_column["type"]).upper():
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE customer_zoho_emails MODIFY COLUMN encrypted_payload_json MEDIUMTEXT NOT NULL")
                )
            logger.info("Expanded customer_zoho_emails.encrypted_payload_json to MEDIUMTEXT.")
        email_index_names = {index["name"] for index in inspector.get_indexes("customer_zoho_emails")}
        if "ix_customer_zoho_emails_zoho_message_id" not in email_index_names:
            with engine.begin() as connection:
                connection.execute(
                    text("CREATE INDEX ix_customer_zoho_emails_zoho_message_id ON customer_zoho_emails (zoho_message_id)")
                )
            logger.info("Added customer_zoho_emails.zoho_message_id index.")
        if "ix_customer_zoho_emails_direction_is_unread_message_id" not in email_index_names:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE INDEX ix_customer_zoho_emails_direction_is_unread_message_id "
                        "ON customer_zoho_emails (direction, is_unread, zoho_message_id)"
                    )
                )
            logger.info("Added customer_zoho_emails unread navigation index.")
        if "ix_customer_zoho_emails_direction_sent_at" not in email_index_names:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE INDEX ix_customer_zoho_emails_direction_sent_at "
                        "ON customer_zoho_emails (direction, zoho_sent_at, id)"
                    )
                )
            logger.info("Added customer_zoho_emails folder ordering index.")
        if "ix_customer_zoho_emails_mailbox_state_direction_sent_at" not in email_index_names:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE INDEX ix_customer_zoho_emails_mailbox_state_direction_sent_at "
                        "ON customer_zoho_emails (mailbox_state, direction, zoho_sent_at, id)"
                    )
                )
            logger.info("Added customer_zoho_emails mailbox-state folder index.")

    if "hub_mailbox_accounts" not in table_names:
        HubMailboxAccount.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_mailbox_accounts table.")

    if "hub_users" in table_names:
        columns = {column["name"] for column in inspector.get_columns("hub_users")}
        if "reminder_email" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE hub_users ADD COLUMN reminder_email VARCHAR(320) NULL"))
            logger.info("Added hub_users.reminder_email column.")

    if "hub_mailbox_emails" not in table_names:
        HubMailboxEmail.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_mailbox_emails table.")
    else:
        columns = {column["name"] for column in inspector.get_columns("hub_mailbox_emails")}
        if "mailbox_state" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE hub_mailbox_emails "
                        "ADD COLUMN mailbox_state VARCHAR(16) NOT NULL DEFAULT 'active' AFTER is_unread"
                    )
                )
            logger.info("Added hub_mailbox_emails.mailbox_state column.")
        mailbox_email_indexes = {index["name"] for index in inspector.get_indexes("hub_mailbox_emails")}
        if "ix_hub_mailbox_emails_mailbox_state_direction_received_at" not in mailbox_email_indexes:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE INDEX ix_hub_mailbox_emails_mailbox_state_direction_received_at "
                        "ON hub_mailbox_emails (mailbox_state, direction, received_at, id)"
                    )
                )
            logger.info("Added hub_mailbox_emails mailbox-state folder index.")

    if "styling_settings" not in table_names:
        StylingSettings.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created styling_settings table.")
    else:
        columns = {column["name"] for column in inspector.get_columns("styling_settings")}
        if "background_secondary_color" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE styling_settings ADD COLUMN background_secondary_color VARCHAR(7) NOT NULL DEFAULT '#efe8da' AFTER background_color")
                )
            logger.info("Added styling_settings.background_secondary_color column.")

    if "module_layouts" not in table_names:
        ModuleLayout.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created module_layouts table.")

    if "zoho_email_workflow_webhooks" not in table_names:
        ZohoEmailWorkflowWebhook.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_email_workflow_webhooks table.")

    if "zoho_email_workflow_deliveries" not in table_names:
        ZohoEmailWorkflowDelivery.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_email_workflow_deliveries table.")

    if "zoho_email_history_imports" not in table_names:
        ZohoEmailHistoryImport.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_email_history_imports table.")

    if "zoho_note_history_imports" not in table_names:
        ZohoNoteHistoryImport.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_note_history_imports table.")

    if "zoho_email_content_imports" not in table_names:
        ZohoEmailContentImport.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_email_content_imports table.")
    else:
        columns = {column["name"] for column in inspector.get_columns("zoho_email_content_imports")}
        additions = {
            "cancel_requested": "TINYINT(1) NOT NULL DEFAULT 0 AFTER status",
            "consecutive_failures": "INT NOT NULL DEFAULT 0 AFTER cancel_requested",
            "continue_automatically": "TINYINT(1) NOT NULL DEFAULT 1 AFTER consecutive_failures",
        }
        missing = [(name, definition) for name, definition in additions.items() if name not in columns]
        if missing:
            with engine.begin() as connection:
                for name, definition in missing:
                    connection.execute(text(f"ALTER TABLE zoho_email_content_imports ADD COLUMN {name} {definition}"))
            logger.info("Added zoho_email_content_imports columns: %s", ", ".join(name for name, _ in missing))

    if "zoho_email_content_import_items" not in table_names:
        ZohoEmailContentImportItem.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_email_content_import_items table.")

    if "zoho_email_attachment_imports" not in table_names:
        ZohoEmailAttachmentImport.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_email_attachment_imports table.")

    if "zoho_email_attachment_import_items" not in table_names:
        ZohoEmailAttachmentImportItem.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created zoho_email_attachment_import_items table.")

    if "hub_mailbox_imap_imports" not in table_names:
        HubMailboxImapImport.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_mailbox_imap_imports table.")

    if "hub_mailbox_imap_import_items" not in table_names:
        HubMailboxImapImportItem.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_mailbox_imap_import_items table.")

    if "hub_mailbox_attachments" not in table_names:
        HubMailboxAttachment.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_mailbox_attachments table.")

    if "hub_mailbox_imap_sync_states" not in table_names:
        HubMailboxImapSyncState.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created hub_mailbox_imap_sync_states table.")
    else:
        columns = {column["name"] for column in inspector.get_columns("hub_mailbox_imap_sync_states")}
        additions = (
            ("last_success_at", "DATETIME NULL"),
            ("consecutive_failures", "INT NOT NULL DEFAULT 0"),
            ("alerted_at", "DATETIME NULL"),
        )
        missing = tuple((name, definition) for name, definition in additions if name not in columns)
        if missing:
            with engine.begin() as connection:
                for name, definition in missing:
                    connection.execute(
                        text(f"ALTER TABLE hub_mailbox_imap_sync_states ADD COLUMN {name} {definition}")
                    )
            logger.info(
                "Added hub_mailbox_imap_sync_states columns: %s",
                ", ".join(name for name, _ in missing),
            )

    if "customer_task_email_reminders" not in table_names:
        CustomerTaskEmailReminder.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created customer_task_email_reminders table.")
    else:
        columns = {column["name"] for column in inspector.get_columns("customer_task_email_reminders")}
        if "minutes_before" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE customer_task_email_reminders "
                        "ADD COLUMN minutes_before INT NOT NULL DEFAULT 0 AFTER sender_email"
                    )
                )
            logger.info("Added customer_task_email_reminders.minutes_before column.")

    if "email_composer_settings" not in table_names:
        EmailComposerSettings.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created email_composer_settings table.")
    else:
        columns = {column["name"] for column in inspector.get_columns("email_composer_settings")}
        with engine.begin() as connection:
            if "signature_html" not in columns:
                connection.execute(text("ALTER TABLE email_composer_settings ADD COLUMN signature_html MEDIUMTEXT NULL"))
                logger.info("Added email_composer_settings.signature_html column.")
            if "line_height" not in columns:
                connection.execute(text("ALTER TABLE email_composer_settings ADD COLUMN line_height DOUBLE NOT NULL DEFAULT 1.1"))
                logger.info("Added email_composer_settings.line_height column.")

    if "email_compose_images" not in table_names:
        EmailComposeImage.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created email_compose_images table.")

    if "user_deletion_batches" not in table_names:
        UserDeletionBatch.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created user_deletion_batches table.")

    if "user_deletion_batch_items" not in table_names:
        UserDeletionBatchItem.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created user_deletion_batch_items table.")

    if "maintenance_runs" in table_names:
        columns = {column["name"] for column in inspector.get_columns("maintenance_runs")}
        if "plugin_installation_package_id" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE maintenance_runs ADD COLUMN plugin_installation_package_id INT NULL")
                )
            logger.info("Added maintenance_runs.plugin_installation_package_id column.")

    if "fleet_refresh_settings" in table_names:
        columns = {column["name"] for column in inspector.get_columns("fleet_refresh_settings")}
        additions = {
            "max_parallel_direct_updates": "INT NOT NULL DEFAULT 5 AFTER max_parallel_site_checks",
            "auto_refresh_enabled": "TINYINT(1) NOT NULL DEFAULT 1 AFTER max_parallel_direct_updates",
            "auto_refresh_interval_hours": "INT NOT NULL DEFAULT 24 AFTER auto_refresh_enabled",
            "auto_refresh_time": "VARCHAR(5) NOT NULL DEFAULT '03:00' AFTER auto_refresh_interval_hours",
            "auto_refresh_next_run_at": "DATETIME NULL AFTER auto_refresh_time",
        }
        missing = [(name, definition) for name, definition in additions.items() if name not in columns]
        if missing:
            with engine.begin() as connection:
                for name, definition in missing:
                    connection.execute(text(f"ALTER TABLE fleet_refresh_settings ADD COLUMN {name} {definition}"))
            logger.info("Added fleet_refresh_settings columns: %s", ", ".join(name for name, _ in missing))

    if "customers" in table_names:
        columns = {column["name"] for column in inspector.get_columns("customers")}
        additions = {
            "zoho_status": "VARCHAR(255) NULL",
            "is_visible": "TINYINT(1) NOT NULL DEFAULT 1",
            "website_domain": "VARCHAR(255) NULL",
            "encrypted_profile_json": "TEXT NULL",
            "zoho_modified_at": "DATETIME NULL",
            "zoho_synced_at": "DATETIME NULL",
        }
        missing = [(name, definition) for name, definition in additions.items() if name not in columns]
        if missing:
            with engine.begin() as connection:
                for name, definition in missing:
                    connection.execute(text(f"ALTER TABLE customers ADD COLUMN {name} {definition}"))
            logger.info("Added Zoho customer columns: %s", ", ".join(name for name, _ in missing))

        inspector = inspect(engine)
        index_names = {index["name"] for index in inspector.get_indexes("customers")}
        customer_indexes = {
            "ix_customers_website_domain": "website_domain",
            "ix_customers_zoho_status": "zoho_status",
            "ix_customers_is_visible": "is_visible",
        }
        for index_name, column_name in customer_indexes.items():
            if index_name in index_names:
                continue
            with engine.begin() as connection:
                connection.execute(text(f"CREATE INDEX {index_name} ON customers ({column_name})"))
            logger.info("Added customers.%s index.", column_name)


def _backfill_customer_zoho_email_headers() -> None:
    """Create the compact encrypted list header for existing imported messages."""
    settings = get_settings()
    updated = 0
    with SessionLocal() as db:
        communications = CustomerCommunicationService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=settings.public_base_url,
        )
        while True:
            emails = db.scalars(
                select(CustomerZohoEmail)
                .where(CustomerZohoEmail.encrypted_header_json == "")
                .order_by(CustomerZohoEmail.id.asc())
                .limit(250)
            ).all()
            if not emails:
                break
            for email in emails:
                payload = communications._payload(email.encrypted_payload_json)
                email.encrypted_header_json = communications._encrypt_email_list_header(payload)
            updated += len(emails)
            db.commit()
            db.expire_all()
    if updated:
        logger.info("Backfilled compact headers for %s customer Zoho email(s).", updated)


def _queue_scheduled_fleet_refresh() -> int | None:
    return FleetRefreshService.queue_scheduled_run()


async def _fleet_update_refresh_loop(initial_delay_seconds: int) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            run_id = await asyncio.to_thread(_queue_scheduled_fleet_refresh)
            if run_id is not None:
                logger.info("Scheduled fleet data refresh cycle started with run %s.", run_id)
        except Exception:
            logger.exception("Fleet update refresh scheduling failed unexpectedly.")
        await asyncio.sleep(60)


async def _fleet_refresh_worker_loop(initial_delay_seconds: int, interval_seconds: int) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            run_id = await asyncio.to_thread(FleetRefreshService.process_next_queued_run)
            if run_id is not None:
                logger.info("Fleet data refresh run %s completed in the background.", run_id)
        except Exception:
            logger.exception("Fleet refresh worker failed unexpectedly.")
        await asyncio.sleep(interval_seconds)


def _poll_maintenance_runs() -> dict[str, int]:
    with SessionLocal() as db:
        service = MaintenanceRunService(db=db, cipher=get_secret_cipher())
        backup_result = service.poll_active_updraftplus_backups(limit=25)
    plugin_update_result = process_pending_direct_updates()
    complete_site_update_result = process_pending_complete_site_updates()
    user_deletion_result = process_pending_user_deletions()
    return {
        "checked": backup_result["checked"] + plugin_update_result["checked"] + complete_site_update_result["checked"] + user_deletion_result["checked"],
        "succeeded": backup_result["succeeded"] + plugin_update_result["succeeded"] + complete_site_update_result["succeeded"] + user_deletion_result["succeeded"],
        "failed": backup_result["failed"] + plugin_update_result["failed"] + complete_site_update_result["failed"] + user_deletion_result["failed"],
        "waiting": backup_result["waiting"] + plugin_update_result["waiting"] + complete_site_update_result["waiting"] + user_deletion_result["waiting"],
        "skipped": plugin_update_result["skipped"] + complete_site_update_result["skipped"] + user_deletion_result["skipped"],
    }


async def _maintenance_run_poll_loop(initial_delay_seconds: int, interval_seconds: int) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            result = await asyncio.to_thread(_poll_maintenance_runs)
            if result["checked"]:
                logger.info("Maintenance polling: %s", result)
        except Exception:
            logger.exception("Maintenance backup polling failed unexpectedly.")
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    async with AsyncExitStack() as stack:
        if settings.auto_create_tables:
            Base.metadata.create_all(bind=engine)
        _ensure_phase_one_schema()
        with SessionLocal() as db:
            HubWorkflowService(db=db).ensure_default_workflows()
            EmailAiPromptPresetService(db=db).ensure_default_presets()
            db.commit()
        _backfill_customer_zoho_email_headers()
        schedule_elapsed_customer_meetings()
        schedule_pending_user_deletions()
        schedule_pending_zoho_email_content_import()
        schedule_pending_zoho_email_attachment_import()
        schedule_pending_hub_mailbox_imap_import()
        schedule_hub_mailbox_imap_inbox_idle()
        schedule_hub_mailbox_imap_sync_polling()
        schedule_pending_zoho_email_history_import()
        schedule_pending_zoho_note_history_import()
        schedule_pending_zoho_email_workflow_deliveries()
        recovered_runs = await asyncio.to_thread(FleetRefreshService.recover_interrupted_runs)
        if recovered_runs:
            logger.info("Re-queued %s interrupted fleet refresh run(s).", recovered_runs)
        await stack.enter_async_context(hub_mcp.session_manager.run())
        fleet_worker_task = asyncio.create_task(
            _fleet_refresh_worker_loop(
                settings.fleet_refresh_worker_initial_delay_seconds,
                settings.fleet_refresh_worker_poll_interval_seconds,
            )
        )
        refresh_task = None
        if settings.fleet_updates_auto_refresh:
            refresh_task = asyncio.create_task(
                _fleet_update_refresh_loop(
                    settings.fleet_updates_initial_delay_seconds,
                )
            )
        maintenance_task = None
        if settings.maintenance_runs_auto_poll:
            maintenance_task = asyncio.create_task(
                _maintenance_run_poll_loop(
                    settings.maintenance_runs_initial_delay_seconds,
                    settings.maintenance_runs_poll_interval_seconds,
                )
            )
        task_email_reminder_worker_task = asyncio.create_task(
            TaskEmailReminderWorker(settings=settings, cipher=get_secret_cipher()).run_forever()
        )
        try:
            yield
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task
            fleet_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await fleet_worker_task
            if maintenance_task is not None:
                maintenance_task.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance_task
            task_email_reminder_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await task_email_reminder_worker_task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.middleware("http")
    async def protect_hub_and_prevent_stale_web_pages(request: Request, call_next):
        mcp_context_token = None
        if _is_mcp_path(request.url.path):
            mcp_actor = _authenticated_mcp_actor(request)
            if mcp_actor is None:
                user = _authenticated_hub_user(request)
                if user is None:
                    return PlainTextResponse(
                        "MCP bearer token or authenticated Hub session required.",
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                    )
                mcp_actor = f"mcp-session:{user.username[:48]}"
                request.state.hub_user = user
            request.state.mcp_actor = mcp_actor
            mcp_context_token = set_mcp_actor(mcp_actor)
        elif _is_desktop_api_path(request.url.path):
            user = _authenticated_desktop_user(request)
            if user is None:
                return PlainTextResponse(
                    "Desktop bearer token required.",
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
            request.state.hub_user = user
        elif not _is_public_hub_path(request.url.path):
            user = _authenticated_hub_user(request)
            if user is None:
                if request.method == "GET" and _prefers_html(request):
                    next_url = request.url.path
                    if request.url.query:
                        next_url = f"{next_url}?{request.url.query}"
                    return RedirectResponse(url=f"/account/login?{urlencode({'next': next_url})}", status_code=303)
                return PlainTextResponse("Authentication required.", status_code=401, headers={"Cache-Control": "no-store"})
            request.state.hub_user = user

        try:
            response = await call_next(request)
        finally:
            if mcp_context_token is not None:
                reset_mcp_actor(mcp_context_token)
        if (
            request.url.path == "/"
            or request.url.path.startswith("/account")
            or request.url.path.startswith("/sites")
            or request.url.path.startswith("/users")
            or request.url.path == "/updates"
            or request.url.path.startswith("/plugin-installations")
            or request.url.path.startswith("/assistant")
            or request.url.path.startswith("/agent")
            or request.url.path.startswith("/api/v1/desktop")
        ):
            # Dynamic inventory, user, and update data must not be served from a browser cache.
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(registrations.router)
    app.include_router(accounts.router)
    app.include_router(desktop_notifications.router)
    app.include_router(accounts.bootstrap_router)
    app.include_router(assistant.router)
    app.include_router(agent.router)
    app.include_router(sites.router)
    app.include_router(site_abilities.router)
    app.include_router(site_backups.router)
    app.include_router(site_inventory.router)
    app.include_router(site_updates.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
    app.mount("/mcp", mcp_asgi_app)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        session_cookie="kosmos_hub_session",
        max_age=12 * 60 * 60,
        same_site="lax",
        https_only=settings.public_base_url.startswith("https://"),
    )
    return app


app = create_app()


def _is_public_hub_path(path: str) -> bool:
    return path in {"/healthz", "/api/v1/registrations", "/account/login", "/account/setup", "/internal/bootstrap-token"} or path.startswith(("/account/zoho/email-workflow-webhook/receive/", "/api/v1/plugin-packages/"))


def _is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _is_desktop_api_path(path: str) -> bool:
    return path == "/api/v1/desktop" or path.startswith("/api/v1/desktop/")


def _authenticated_mcp_actor(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    with SessionLocal() as db:
        service = HubAccountService(db=db, app_secret_key=get_settings().app_secret_key)
        authenticated = service.authenticate_mcp_access_token(token)
        if authenticated is None:
            return None
        user, access_token = authenticated
        return f"mcp:{user.username[:48]}:{access_token.id}"


def _authenticated_hub_user(request: Request):
    user_id = request.session.get("user_id")
    session_version = request.session.get("session_version")
    if not isinstance(user_id, int) or not isinstance(session_version, int):
        return None
    with SessionLocal() as db:
        service = HubAccountService(db=db, app_secret_key=get_settings().app_secret_key)
        user = service.get_user(user_id)
        if user is None or not user.is_active or user.session_version != session_version:
            return None
        db.expunge(user)
        return user


def _authenticated_desktop_user(request: Request):
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    with SessionLocal() as db:
        service = HubAccountService(db=db, app_secret_key=get_settings().app_secret_key)
        authenticated = service.authenticate_desktop_device(token)
        if authenticated is None:
            return None
        user, _device = authenticated
        db.expunge(user)
        return user


def _prefers_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")
