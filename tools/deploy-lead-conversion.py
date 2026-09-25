"""Guarded additive-schema release; all application files come from Git archives."""
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
before = Path('/tmp/lead-conversion-before.tar')
after = Path('/tmp/lead-conversion-release.tar')
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
assert old.keys() <= new.keys()
changes = {name for name in new if old.get(name) != new[name]}
assert changes == expected, changes
for name, data in old.items():
    assert (root / name).read_bytes().replace(b'\r\n', b'\n') == data.replace(b'\r\n', b'\n'), name + ': production changed'

os.chdir(root)
sys.path.insert(0, str(root))
from dotenv import load_dotenv
load_dotenv('/etc/kosmos-hub/kosmos-hub.env')
from sqlalchemy import select, func, text
from app.db.session import SessionLocal
from app.models.maintenance_run import MaintenanceRun
from app.models.fleet_refresh_run import FleetRefreshRun
from app.models.hub_wordpress_job import HubWordPressJob
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_scheduled_email import HubScheduledEmail

with SessionLocal() as db:
    db.execute(text('SET TRANSACTION READ ONLY'))
    active = {model.__tablename__: db.scalar(select(func.count()).select_from(model).where(model.status.in_(states)))
              for model, states in [(MaintenanceRun, ('running',)),
                                    (FleetRefreshRun, ('queued', 'running', 'cancelling')),
                                    (HubWordPressJob, ('queued', 'running')),
                                    (HubFinanceGeneratedPdf, ('queued', 'rendering')),
                                    (CustomerTaskEmailReminder, ('sending',)),
                                    (HubScheduledEmail, ('sending',))]}
assert not any(active.values()), 'Active jobs: ' + json.dumps(active)
print(json.dumps({'verified_runtime_files': len(old), 'changes': sorted(changes), 'active_jobs': active}), flush=True)
if '--apply' not in sys.argv:
    raise SystemExit(0)

release = 'lead-conversion-' + time.strftime('%Y%m%d-%H%M%S')
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
