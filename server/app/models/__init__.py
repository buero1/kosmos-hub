"""Database models."""

from app.models.ai_provider_config import AiProviderConfig
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerEmailAttachment, CustomerZohoEmail, CustomerZohoEmailImage, CustomerZohoNote
from app.models.email_compose_image import EmailComposeImage
from app.models.email_composer_settings import EmailComposerSettings
from app.models.email_ai_prompt_preset import EmailAiPromptPreset
from app.models.customer_activity import CustomerCallActivity, CustomerCallReminder, CustomerMeetingActivity, CustomerMeetingReminder, CustomerTaskActivity
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_mailbox_email import HubMailboxAttachment, HubMailboxEmail
from app.models.hub_spam_sender import HubSpamSender
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_imap_import import HubMailboxImapImport, HubMailboxImapImportItem
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.models.hub_mailbox_imap_sync_failure import HubMailboxImapSyncFailure
from app.models.hub_access_token import HubAccessToken
from app.models.hub_integration_token import HubIntegrationToken
from app.models.hub_desktop_device import HubDesktopDevice
from app.models.hub_setup_token import HubSetupToken
from app.models.hub_user import HubUser
from app.models.hub_case import HubCase
from app.models.hub_case_email_link import HubCaseEmailLink
from app.models.hub_lead import HubLead
from app.models.hub_lead_email import HubLeadEmail
from app.models.hub_lead_note import HubLeadNote
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_offer import HubFinanceOffer, HubFinanceOfferLine
from app.models.hub_finance_documents import (
    HubFinanceDunning,
    HubFinanceDunningLine,
    HubFinanceInvoice,
    HubFinanceInvoiceLine,
    HubFinanceOrder,
    HubFinanceOrderLine,
    HubFinanceRecurringInvoice,
    HubFinanceRecurringInvoiceLine,
)
from app.models.hub_finance_order_pdf import HubFinanceOrderPdf
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_finance_position_preset import HubFinancePositionPreset
from app.models.hub_legal_terms import HubLegalTerms, HubLegalTermsRevision
from app.models.hub_pdf_template import HubPdfTemplate, HubPdfTemplateRevision
from app.models.hub_agent import HubAgentAction, HubAgentConversation, HubAgentConversationContext, HubAgentJob
from app.models.hub_workflow import HubWorkflow
from app.models.fleet_refresh_run import FleetRefreshRun, FleetRefreshSiteResult
from app.models.fleet_refresh_settings import FleetRefreshSettings
from app.models.maintenance_run import MaintenanceRun, MaintenanceRunStep
from app.models.module_layout import ModuleLayout
from app.models.plugin_installation_package import PluginInstallationPackage
from app.models.provider_credential import ProviderCredential
from app.models.plugin_official_version import PluginOfficialVersion
from app.models.site_user_snapshot import SiteUserSnapshot
from app.models.styling_settings import StylingSettings
from app.models.user_deletion_batch import UserDeletionBatch, UserDeletionBatchItem
from app.models.zoho_email_template import ZohoEmailTemplate
from app.models.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhook
from app.models.zoho_email_content_import import ZohoEmailContentImport, ZohoEmailContentImportItem
from app.models.zoho_email_attachment_import import ZohoEmailAttachmentImport, ZohoEmailAttachmentImportItem
from app.models.zoho_email_history_import import ZohoEmailHistoryImport
from app.models.zoho_note_history_import import ZohoNoteHistoryImport
from app.models.zoho_connection import ZohoConnection
from app.models.zoho_books_connection import ZohoBooksConnection
from app.models.zoho_books_order_import import ZohoBooksOrderImport, ZohoBooksOrderImportItem
from app.models.zoho_books_recurring_invoice_import import (
    ZohoBooksRecurringInvoiceImport,
    ZohoBooksRecurringInvoiceImportItem,
)

__all__ = ["AiProviderConfig", "CustomerActivityReminderNotification", "CustomerCallActivity", "CustomerCallReminder", "CustomerContact", "CustomerEmailAttachment", "CustomerMeetingActivity", "CustomerMeetingReminder", "CustomerTaskActivity", "CustomerTaskEmailReminder", "CustomerZohoEmail", "CustomerZohoEmailImage", "CustomerZohoNote", "EmailComposeImage", "EmailComposerSettings", "FleetRefreshRun", "FleetRefreshSettings", "FleetRefreshSiteResult", "HubAccessToken", "HubIntegrationToken", "HubAgentAction", "HubAgentJob", "HubCase", "HubCaseEmailLink", "HubDesktopDevice", "HubFinanceArticle", "HubFinanceDunning", "HubFinanceDunningLine", "HubFinanceGeneratedPdf", "HubFinanceInvoice", "HubFinanceInvoiceLine", "HubFinanceOffer", "HubFinanceOfferLine", "HubFinanceOrder", "HubFinanceOrderLine", "HubFinanceOrderPdf", "HubFinancePositionPreset", "HubFinanceRecurringInvoice", "HubFinanceRecurringInvoiceLine", "HubLead", "HubLeadEmail", "HubLeadNote", "HubLegalTerms", "HubLegalTermsRevision", "HubMailboxAccount", "HubMailboxAttachment", "HubMailboxEmail", "HubMailboxImapImport", "HubMailboxImapImportItem", "HubMailboxImapSyncFailure", "HubMailboxImapSyncState", "HubPdfTemplate", "HubPdfTemplateRevision", "HubSetupToken", "HubUser", "HubWorkflow", "MaintenanceRun", "MaintenanceRunStep", "ModuleLayout", "PluginInstallationPackage", "PluginOfficialVersion", "ProviderCredential", "SiteUserSnapshot", "StylingSettings", "UserDeletionBatch", "UserDeletionBatchItem", "ZohoBooksConnection", "ZohoBooksOrderImport", "ZohoBooksOrderImportItem", "ZohoBooksRecurringInvoiceImport", "ZohoBooksRecurringInvoiceImportItem", "ZohoConnection", "ZohoEmailAttachmentImport", "ZohoEmailAttachmentImportItem", "ZohoEmailContentImport", "ZohoEmailContentImportItem", "ZohoEmailTemplate", "ZohoEmailHistoryImport", "ZohoNoteHistoryImport", "ZohoEmailWorkflowWebhook"]
