"""Guarded conversion releases; deploy only verified Git archives."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen

root = Path('/opt/kosmos-hub/app/server')
release_prefix = 'lead-manual-dates' if '--date-correction' in sys.argv else 'lead-conversion'
if '--offer-order' in sys.argv:
    release_prefix = 'offer-to-order'
if '--invoice-delivery' in sys.argv:
    release_prefix = 'invoice-delivery'
if '--invoice-compose' in sys.argv:
    release_prefix = 'invoice-compose'
if '--finance-relations' in sys.argv:
    release_prefix = 'finance-relations'
if '--recurring-status' in sys.argv:
    release_prefix = 'recurring-status'
if '--order-placeholders' in sys.argv:
    release_prefix = 'order-placeholders'
if '--order-compose' in sys.argv:
    release_prefix = 'order-compose'
if '--order-compose-polish' in sys.argv:
    release_prefix = 'order-compose-polish'
if '--order-compose-template' in sys.argv:
    release_prefix = 'order-compose-template'
if '--bridge-recovery' in sys.argv:
    release_prefix = 'bridge-recovery'
if '--invoice-recipient' in sys.argv:
    release_prefix = 'invoice-recipient'
if '--email-preview' in sys.argv:
    release_prefix = 'email-preview'
if '--local-crm' in sys.argv:
    release_prefix = 'local-crm'
if '--email-reply-urls' in sys.argv:
    release_prefix = 'email-reply-urls'
if '--note-titles' in sys.argv:
    release_prefix = 'note-titles'
if '--recipient-access' in sys.argv:
    release_prefix = 'recipient-access'
if '--sepa-webhook' in sys.argv:
    release_prefix = 'sepa-webhook'
if '--email-send-navigation' in sys.argv:
    release_prefix = 'email-send-navigation'
if '--sepa-diagnostics' in sys.argv:
    release_prefix = 'sepa-diagnostics'
if '--sepa-field-labels' in sys.argv:
    release_prefix = 'sepa-field-labels'
if '--customer-iban-reveal' in sys.argv:
    release_prefix = 'customer-iban-reveal'
if '--sepa-grant-method' in sys.argv:
    release_prefix = 'sepa-grant-method'
if '--lead-url-fields' in sys.argv:
    release_prefix = 'lead-url-fields'
if '--lead-appointment-email' in sys.argv:
    release_prefix = 'lead-appointment-email'
if '--contact-customer' in sys.argv:
    release_prefix = 'contact-customer'
if '--lead-datetime-control' in sys.argv:
    release_prefix = 'lead-datetime-control'
if '--record-email-return' in sys.argv:
    release_prefix = 'record-email-return'
if '--activity-delete-position' in sys.argv:
    release_prefix = 'activity-delete-position'
if '--customer-checklists' in sys.argv:
    release_prefix = 'customer-checklists'
if '--customer-checklists-fix' in sys.argv:
    release_prefix = 'customer-checklists-fix'
if '--customer-checklists-collapse' in sys.argv:
    release_prefix = 'customer-checklists-collapse'
if '--customer-checklist-defaults' in sys.argv:
    release_prefix = 'customer-checklist-defaults'
if '--customer-industry-combobox' in sys.argv:
    release_prefix = 'customer-industry-combobox'
if '--customer-industry-cleanup' in sys.argv:
    release_prefix = 'customer-industry-cleanup'
if '--lead-industry-combobox' in sys.argv:
    release_prefix = 'lead-industry-combobox'
if '--site-seo' in sys.argv:
    release_prefix = 'site-seo'
if '--lead-homepage-option' in sys.argv:
    release_prefix = 'lead-homepage-option'
if '--activity-relation-link' in sys.argv:
    release_prefix = 'activity-relation-link'
if '--call-directory-filter' in sys.argv:
    release_prefix = 'call-directory-filter'
if '--finance-directory-search' in sys.argv:
    release_prefix = 'finance-directory-search'
if '--mailbox-invoices' in sys.argv:
    release_prefix = 'mailbox-invoices'
if '--mailbox-invoices-aggregate' in sys.argv:
    release_prefix = 'mailbox-invoices-aggregate'
if '--mailbox-invoices-websites' in sys.argv:
    release_prefix = 'mailbox-invoices-websites'
if '--email-template-placeholder-link' in sys.argv:
    release_prefix = 'email-template-placeholder-link'
if '--finance-discount-summary' in sys.argv:
    release_prefix = 'finance-discount-summary'
if '--mailbox-search' in sys.argv:
    release_prefix = 'mailbox-search'
if '--mailbox-search-hotfix' in sys.argv:
    release_prefix = 'mailbox-search-hotfix'
if '--mailbox-reply-sender' in sys.argv:
    release_prefix = 'mailbox-reply-sender'
if '--mailbox-recipient-sender' in sys.argv:
    release_prefix = 'mailbox-recipient-sender'
if '--mailbox-health-disabled' in sys.argv:
    release_prefix = 'mailbox-health-disabled'
before = Path('/tmp/' + release_prefix + '-before.tar')
after = Path('/tmp/' + release_prefix + '-release.tar')
assert hashlib.sha256(after.read_bytes()).hexdigest() == sys.argv[1]


def archive_files(path):
    with tarfile.open(path) as archive:
        entries = archive.getmembers()
        assert all((entry.isdir() or entry.isfile()) and not entry.name.startswith('/')
                   and '..' not in Path(entry.name).parts for entry in entries)
        files = {entry.name.removeprefix('server/'): archive.extractfile(entry).read()
                 for entry in entries if entry.isfile()}
        assert files and all(name.startswith('app/') for name in files)
        return files


old, new = archive_files(before), archive_files(after)
expected = {
    'app/core/templates.py', 'app/db/base.py', 'app/main.py', 'app/models/hub_lead_conversion.py',
    'app/services/customer_directory.py', 'app/services/hub_conversion_info.py',
    'app/services/hub_email_associations.py', 'app/services/hub_lead_conversion.py',
    'app/services/hub_leads.py', 'app/services/hub_mailbox.py', 'app/services/hub_mailbox_access.py',
    'app/services/hub_workflows.py', 'app/templates/partials/hub_data_panel.html',
}
if '--date-correction' in sys.argv:
    expected = {'app/services/hub_workflows.py'}
if '--offer-order' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/main.py', 'app/models/hub_finance_documents.py',
        'app/services/hub_finance_documents.py', 'app/services/hub_finance_offer_conversion.py',
        'app/services/hub_finance_operations_shared.py', 'app/services/hub_operation_offers.py',
        'app/templates/finance_document_detail.html', 'app/templates/finance_offer_detail.html',
        'app/templates/partials/finance_customer_search_script.html',
        'app/templates/partials/finance_document_positions.html',
    }
if '--local-crm' not in sys.argv:
    assert old.keys() <= new.keys()
if '--invoice-delivery' in sys.argv:
    expected = {
        'app/main.py', 'app/models/hub_invoice_email_batch.py',
        'app/services/hub_finance_documents.py', 'app/services/hub_invoice_email_batches.py',
        'app/services/hub_invoice_email_delivery.py',
        'app/templates/finance_documents.html', 'app/templates/finance_document_detail.html',
    }
if '--invoice-compose' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/hub_invoice_email_batches.py',
        'app/services/hub_mailbox.py', 'app/services/hub_operation_invoice_email.py',
        'app/templates/finance_document_detail.html', 'app/templates/base.html',
        'app/templates/emails.html',
    }
changes = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
if '--email-send-navigation' in sys.argv:
    expected = {'app/static/email-delivery.js', 'app/templates/base.html', 'app/templates/emails.html'}
if '--sepa-diagnostics' in sys.argv:
    expected = {'app/api/routes/sepa.py', 'app/services/hub_sepa.py'}
if '--sepa-field-labels' in sys.argv:
    expected = {'app/services/hub_sepa.py'}
if '--sepa-grant-method' in sys.argv:
    expected = {'app/services/hub_sepa.py'}
if '--lead-url-fields' in sys.argv:
    expected = {'app/core/web_urls.py', 'app/services/customer_directory.py',
                'app/services/hub_leads.py', 'app/templates/lead_detail.html'}
if '--lead-appointment-email' in sys.argv:
    expected = {
        'app/main.py', 'app/models/hub_scheduled_email.py',
        'app/services/hub_workflows.py',
        'app/services/lead_appointment_email_reminders.py',
        'app/services/scheduled_email_worker.py',
        'app/services/scheduled_emails.py',
    }
if '--contact-customer' in sys.argv:
    expected = {
        'app/api/routes/web.py',
        'app/services/customer_directory.py',
        'app/services/hub_operation_contacts.py',
        'app/templates/customer_contact_detail.html',
    }
if '--lead-datetime-control' in sys.argv:
    expected = {'app/templates/partials/lead_field_control.html'}
if '--record-email-return' in sys.argv:
    expected = {
        'app/api/routes/web.py',
        'app/templates/base.html',
        'app/templates/lead_detail.html',
    }
if '--activity-delete-position' in sys.argv:
    expected = {
        'app/templates/base.html',
        'app/templates/partials/customer_activity_composer.html',
    }
if '--customer-checklists' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/db/base.py', 'app/main.py',
        'app/models/__init__.py', 'app/models/customer.py',
        'app/models/customer_checklist.py', 'app/services/customer_checklists.py',
        'app/services/customer_directory.py', 'app/static/customer-checklists.js',
        'app/templates/base.html', 'app/templates/customer_detail.html',
        'app/templates/partials/customer_checklists.html',
    }
if '--customer-checklists-fix' in sys.argv:
    expected = {'app/api/routes/web.py', 'app/static/customer-checklists.js'}
if '--customer-checklists-collapse' in sys.argv:
    expected = {'app/templates/base.html', 'app/templates/partials/customer_checklists.html'}
if '--customer-checklist-defaults' in sys.argv:
    expected = {'app/main.py', 'app/models/customer.py', 'app/services/customer_checklists.py'}
if '--customer-industry-combobox' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/customer_directory.py',
        'app/services/hub_profile_values.py', 'app/templates/customer_create.html',
        'app/templates/customer_detail.html',
    }
if '--customer-industry-cleanup' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/customer_directory.py',
        'app/services/hub_leads.py',
    }
if '--lead-industry-combobox' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/hub_leads.py',
        'app/templates/lead_create.html', 'app/templates/lead_detail.html',
        'app/templates/partials/lead_field_control.html',
    }
if '--site-seo' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/site_seo.py',
        'app/services/hub_operation_wordpress.py', 'app/services/wordpress_jobs.py',
        'app/services/wordpress_remote_catalog.py', 'app/templates/site_detail.html',
        'app/templates/wordpress_job.html',
    }
if '--lead-homepage-option' in sys.argv:
    expected = {'app/services/hub_lead_field_catalog.py'}
if '--activity-relation-link' in sys.argv:
    expected = {
        'app/templates/base.html',
        'app/templates/partials/activity_party_search.html',
    }
if '--call-directory-filter' in sys.argv:
    expected = {
        'app/api/routes/web.py',
        'app/services/hub_crm_readers.py',
        'app/templates/base.html',
        'app/templates/partials/activity_view_filter.html',
    }
if '--finance-directory-search' in sys.argv:
    expected = {
        'app/api/routes/web.py',
        'app/static/finance-directory-search.js',
        'app/templates/base.html',
        'app/templates/finance_articles.html',
        'app/templates/finance_documents.html',
        'app/templates/finance_offers.html',
        'app/templates/partials/finance_directory_search.html',
    }
if '--mailbox-invoices' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/hub_mailbox.py',
        'app/services/hub_operation_mailbox.py', 'app/templates/emails.html',
        'app/templates/emails_message_list.html',
    }
if '--mailbox-invoices-aggregate' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/hub_mailbox.py', 'app/templates/emails.html',
    }
if '--mailbox-invoices-websites' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/hub_mailbox.py',
        'app/services/hub_operation_mailbox.py', 'app/templates/emails.html',
    }
if '--email-template-placeholder-link' in sys.argv:
    expected = {'app/services/customer_communications.py'}
if '--finance-discount-summary' in sys.argv:
    expected = {
        'app/templates/finance_document_detail.html',
        'app/templates/finance_generated_pdf.html',
        'app/templates/finance_offer_detail.html',
        'app/templates/partials/finance_document_positions.html',
        'app/templates/partials/finance_offer_positions.html',
    }
if '--mailbox-search' in sys.argv:
    expected = {
        'app/templates/emails.html',
        'app/templates/emails_message_list.html',
    }
if '--mailbox-search-hotfix' in sys.argv:
    expected = {'app/templates/emails.html'}
if '--mailbox-reply-sender' in sys.argv:
    expected = {'app/services/hub_email_composition.py', 'app/templates/base.html'}
if '--mailbox-recipient-sender' in sys.argv:
    expected = {
        'app/main.py', 'app/models/hub_mailbox_account.py',
        'app/services/hub_email_composition.py', 'app/services/hub_mailbox_accounts.py',
        'app/services/hub_mailbox_health.py', 'app/services/hub_mailbox_imap_import.py',
        'app/services/hub_mailbox_imap_sync.py', 'app/services/maintenance_worker.py',
        'app/templates/account.html', 'app/templates/emails.html',
    }
if '--mailbox-health-disabled' in sys.argv:
    expected = {'app/api/routes/web.py', 'app/services/hub_mailbox_imap_sync.py'}
if '--customer-iban-reveal' in sys.argv:
    expected = {'app/api/routes/web.py', 'app/services/hub_customer_iban.py',
                'app/templates/customer_detail.html', 'app/static/customer-iban.js', 'app/static/customer-iban.css'}
if '--sepa-webhook' in sys.argv:
    expected = {
        'app/api/routes/sepa.py', 'app/db/base.py', 'app/main.py',
        'app/models/hub_sepa_submission.py', 'app/services/hub_sepa.py',
        'app/services/customer_communications.py', 'app/services/template_placeholders.py',
    }
if '--recipient-access' in sys.argv:
    expected = {'app/services/hub_email_readers.py', 'app/services/customer_communications.py'}
if '--email-reply-urls' in sys.argv:
    expected = {'app/services/customer_communications.py'}
if '--note-titles' in sys.argv:
    expected = {
        'app/services/hub_note_catalog.py', 'app/services/customer_communications.py',
        'app/services/hub_lead_notes.py', 'app/services/hub_operation_notes.py',
        'app/templates/customer_detail.html', 'app/templates/lead_detail.html', 'app/templates/base.html',
    }
if '--finance-relations' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/hub_finance_operations_shared.py',
        'app/services/hub_operation_finance.py',
        'app/templates/partials/finance_customer_search_script.html',
    }
if '--recurring-status' in sys.argv:
    expected = {
        'app/services/hub_finance_documents.py', 'app/services/hub_recurring_invoice_generation.py',
        'app/services/zoho_books_recurring_invoice_import.py', 'app/services/hub_operation_finance.py',
        'app/templates/partials/finance_recurring_interval_script.html',
    }
if '--order-placeholders' in sys.argv:
    expected = {
        'app/services/template_placeholders.py', 'app/services/hub_finance_pdf_generation.py',
    }
if '--order-compose' in sys.argv:
    expected = {
        'app/api/routes/web.py', 'app/services/hub_access_control.py',
        'app/services/customer_communications.py', 'app/services/hub_mailbox.py',
        'app/services/hub_operation_order_email.py', 'app/templates/base.html',
        'app/services/hub_template_contexts.py',
        'app/templates/emails.html', 'app/templates/finance_document_detail.html',
    }
if '--order-compose-polish' in sys.argv:
    expected = {'app/services/hub_operation_order_email.py', 'app/templates/base.html'}
if '--order-compose-template' in sys.argv:
    expected = {'app/services/hub_operation_order_email.py', 'app/templates/base.html'}
if '--bridge-recovery' in sys.argv:
    expected = {'app/services/maintenance_runs.py'}
if '--invoice-recipient' in sys.argv:
    expected = {
        'app/services/customer_communications.py', 'app/services/hub_invoice_email_batches.py',
        'app/services/hub_operation_invoice_email.py',
    }
if '--email-preview' in sys.argv:
    expected = {
        'app/services/customer_communications.py', 'app/services/hub_mailbox.py',
        'app/services/hub_mailbox_imap_import.py', 'app/services/hub_cases.py',
        'app/services/hub_lead_emails.py',
    }
if '--local-crm' in sys.argv:
    expected = {
        'app/api/routes/accounts.py', 'app/api/routes/web.py', 'app/main.py',
        'app/services/customer_communications.py', 'app/services/customer_directory.py',
        'app/services/customer_profile.py', 'app/services/hub_customer_field_catalog.py',
        'app/services/hub_cases.py',
        'app/services/hub_lead_conversion.py', 'app/services/hub_lead_emails.py',
        'app/services/hub_lead_notes.py', 'app/services/hub_leads.py',
        'app/services/hub_operation_contacts.py', 'app/services/hub_operation_email_composition.py',
        'app/services/hub_operation_notes.py', 'app/services/hub_operation_records.py',
        'app/services/hub_profile_values.py', 'app/services/hub_record_catalog.py',
        'app/services/maintenance_worker.py', 'app/services/retire_external_crm.py',
        'app/services/zoho_books.py', 'app/services/zoho_books_invoice_import.py',
        'app/services/zoho_books_order_import.py', 'app/services/zoho_books_order_pdf_import.py',
        'app/services/zoho_books_recurring_invoice_import.py', 'app/services/zoho_case_import.py',
        'app/services/zoho_crm.py', 'app/services/zoho_email_attachment_import.py',
        'app/services/zoho_email_content_import.py', 'app/services/zoho_email_history_import.py',
        'app/services/zoho_email_workflow_webhook.py', 'app/services/zoho_lead_import.py',
        'app/services/zoho_note_history_import.py', 'app/templates/account.html',
        'app/templates/cases.html', 'app/templates/customer_contact_detail.html',
        'app/templates/case_detail.html', 'app/templates/emails_reading_pane.html',
        'app/templates/customer_detail.html', 'app/templates/lead_detail.html',
        'app/templates/leads.html', 'app/templates/partials/contact_field_control.html',
        'app/templates/partials/lead_field_control.html',
    }
    assert set(old) - set(new) == {name for name in expected if name.startswith('app/services/zoho_')}
assert changes == expected, changes
if any(flag in sys.argv for flag in ('--invoice-recipient', '--email-preview', '--local-crm', '--email-reply-urls', '--note-titles', '--recipient-access', '--sepa-webhook', '--email-send-navigation', '--sepa-diagnostics', '--sepa-field-labels', '--customer-iban-reveal', '--sepa-grant-method', '--lead-url-fields', '--lead-appointment-email', '--contact-customer', '--lead-datetime-control', '--record-email-return', '--activity-delete-position', '--customer-checklists', '--customer-checklists-fix', '--customer-checklists-collapse', '--customer-checklist-defaults', '--customer-industry-combobox', '--customer-industry-cleanup', '--lead-industry-combobox', '--site-seo', '--lead-homepage-option', '--activity-relation-link', '--call-directory-filter', '--finance-directory-search', '--mailbox-invoices', '--mailbox-invoices-aggregate', '--mailbox-invoices-websites', '--email-template-placeholder-link', '--finance-discount-summary', '--mailbox-search', '--mailbox-search-hotfix', '--mailbox-reply-sender', '--mailbox-recipient-sender', '--mailbox-health-disabled')):
    runtime_files = {str(path.relative_to(root)) for path in (root / 'app').rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    assert runtime_files == set(old), 'Unexpected runtime files: ' + str(runtime_files ^ set(old))
for name, data in old.items():
    assert (root / name).read_bytes().replace(b'\r\n', b'\n') == data.replace(b'\r\n', b'\n'), name + ': production changed'

os.chdir(root)
sys.path.insert(0, str(root))
from dotenv import load_dotenv
load_dotenv('/etc/kosmos-hub/kosmos-hub.env')
from sqlalchemy import select, func, text
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.maintenance_run import MaintenanceRun
from app.models.fleet_refresh_run import FleetRefreshRun
from app.models.hub_wordpress_job import HubWordPressJob
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_scheduled_email import HubScheduledEmail
from app.models.hub_invoice_email_batch import HubInvoiceEmailBatch

with SessionLocal() as db:
    db.execute(text('SET TRANSACTION READ ONLY'))
    active = {model.__tablename__: db.scalar(select(func.count()).select_from(model).where(model.status.in_(states)))
              for model, states in [(MaintenanceRun, ('running',)),
                                    (FleetRefreshRun, ('queued', 'running', 'cancelling')),
                                    (HubWordPressJob, ('queued', 'running')),
                                    (HubFinanceGeneratedPdf, ('queued', 'rendering')),
                                    (CustomerTaskEmailReminder, ('sending',)),
                                    (HubScheduledEmail, ('sending',)),
                                    (HubInvoiceEmailBatch, ('queued', 'running'))]}
assert not any(active.values()), 'Active jobs: ' + json.dumps(active)
print(json.dumps({'verified_runtime_files': len(old), 'changes': sorted(changes), 'active_jobs': active}), flush=True)
if '--apply' not in sys.argv:
    raise SystemExit(0)

release = release_prefix + '-' + time.strftime('%Y%m%d-%H%M%S')
stage = Path('/opt/kosmos-hub/staging') / release
backup = Path('/opt/kosmos-hub/backups') / ('app-before-' + release)
stage.mkdir(parents=True)
assert not backup.exists()
with tarfile.open(after) as archive:
    archive.extractall(stage, filter='data')
staged_app = stage / 'server/app'
account = pwd.getpwnam('kosmos-hub')
for path in (staged_app, *staged_app.rglob('*')):
    os.chown(path, account.pw_uid, account.pw_gid)
    os.chmod(path, 0o755 if path.is_dir() else 0o644)


def healthy():
    for _ in range(45):
        try:
            with urlopen('http://127.0.0.1:8102/healthz', timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Health check failed')


subprocess.run(['systemctl', 'stop', 'kosmos-hub-api'], check=True)
try:
    (root / 'app').rename(backup)
    staged_app.rename(root / 'app')
    subprocess.run(['systemctl', 'start', 'kosmos-hub-api'], check=True)
    healthy()
except Exception:
    subprocess.run(['systemctl', 'stop', 'kosmos-hub-api'], check=True)
    if backup.exists():
        if (root / 'app').exists():
            (root / 'app').rename(stage / 'failed-app')
        backup.rename(root / 'app')
    subprocess.run(['systemctl', 'start', 'kosmos-hub-api'], check=True)
    healthy()
    raise
subprocess.run(['systemctl', 'is-active', 'kosmos-hub-api'], check=True)
if '--lead-appointment-email' in sys.argv:
    from sqlalchemy import inspect
    schema = inspect(engine)
    columns = {column['name'] for column in schema.get_columns('hub_scheduled_emails')}
    indexes = {index['name'] for index in schema.get_indexes('hub_scheduled_emails')}
    assert 'automation_key' in columns
    assert 'uq_hub_scheduled_emails_automation_key' in indexes
if '--customer-checklists' in sys.argv:
    from sqlalchemy import inspect
    schema = inspect(engine)
    customer_columns = {column['name'] for column in schema.get_columns('customers')}
    assert 'checklists_initialized' in customer_columns
    assert {'customer_checklists', 'customer_checklist_items'} <= set(schema.get_table_names())
    with engine.connect() as connection:
        pending = connection.scalar(text('SELECT COUNT(*) FROM customers WHERE checklists_initialized = 0'))
        checklist_count = connection.scalar(text('SELECT COUNT(*) FROM customer_checklists'))
    assert pending == 0
    assert checklist_count > 0
if '--customer-checklist-defaults' in sys.argv:
    from sqlalchemy import inspect
    schema = inspect(engine)
    customer_columns = {column['name'] for column in schema.get_columns('customers')}
    assert 'checklists_template_version' in customer_columns
    with engine.connect() as connection:
        customers = connection.scalar(text('SELECT COUNT(*) FROM customers'))
        pending = connection.scalar(text('SELECT COUNT(*) FROM customers WHERE checklists_template_version < 2'))
        post_review = connection.scalar(text("SELECT COUNT(*) FROM customer_checklists WHERE title = 'Nach Kundensicht'"))
        final_setup = connection.scalar(text("SELECT COUNT(*) FROM customer_checklists WHERE title = 'Letzte Einrichtungen'"))
    assert pending == 0
    assert post_review >= customers
    assert final_setup >= customers
print(json.dumps({'deployed': True, 'health': 200, 'backup': str(backup)}), flush=True)
