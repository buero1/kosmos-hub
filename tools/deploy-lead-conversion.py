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
if any(flag in sys.argv for flag in ('--invoice-recipient', '--email-preview', '--local-crm', '--email-reply-urls', '--note-titles', '--recipient-access', '--sepa-webhook', '--email-send-navigation')):
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
from app.db.session import SessionLocal
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
print(json.dumps({'deployed': True, 'health': 200, 'backup': str(backup)}), flush=True)
