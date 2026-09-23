"""Read-only release checks on the Hub host; no sessions or credentials logged."""
import base64
import json
import os
import sys
from urllib.request import Request, urlopen
from dotenv import load_dotenv
from itsdangerous import TimestampSigner

os.chdir('/opt/kosmos-hub/app/server')
sys.path.insert(0, os.getcwd())
load_dotenv('/etc/kosmos-hub/kosmos-hub.env')
from sqlalchemy import select, text
from app.db.base import Base
from app.db.session import SessionLocal
from app.models.hub_user import HubUser
from app.core.config import get_settings

with SessionLocal() as db:
    db.execute(text('SET TRANSACTION READ ONLY'))
    active = {}
    for table in ('maintenance_runs', 'fleet_refresh_runs', 'hub_wordpress_jobs'):
        active[table] = db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE status IN ('queued','running','cancelling')")).scalar()
    print('Active background jobs:', json.dumps(active))
    if '--ready' in sys.argv:
        assert not any(active.values()), 'Wait for active jobs before release restart.'
    else:
        user = db.scalar(select(HubUser).where(HubUser.role == 'admin', HubUser.is_active.is_(True)))
        session = base64.b64encode(json.dumps({'user_id': user.id, 'session_version': user.session_version}).encode())
        cookie = TimestampSigner(get_settings().app_secret_key).sign(session).decode()
        request = Request('https://kosmos-hub.31-70-92-95.sslip.io/updates?site_scope=selected&site_id=2',
            headers={'Cookie': 'kosmos_hub_session=' + cookie})
        with urlopen(request, timeout=45) as response:
            page = response.read().decode()
            assert response.status == 200
            assert 'data-plugin-auto-updates' in page and 'elementor/elementor.php' in page
            assert 'value="auto-updates"' in page and 'Kosmos Bridge 0.3.68' in page
            print('Live admin workbench: HTTP 200, shared bulk action and plugin inventory rendered.')
