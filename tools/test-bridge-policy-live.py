"""Signed roundtrip on the dedicated test site only, restoring the policy."""
import os
import sys
from dotenv import load_dotenv

os.chdir('/opt/kosmos-hub/app/server')
sys.path.insert(0, os.getcwd())
load_dotenv('/etc/kosmos-hub/kosmos-hub.env')
from app.db.base import Base
from app.db.session import SessionLocal
from app.core.security import get_secret_cipher
from app.models.site import Site
from app.services.site_mcp_proxy import SiteMcpProxyService

with SessionLocal() as db:
    site = db.get(Site, 2)
    assert site.domain == 'test-gasthofloewen.kosmos-medien.de'
    proxy = SiteMcpProxyService(db=db, cipher=get_secret_cipher())
    plugin = 'kosmos-content-kit/kosmos-content-kit.php'
    inputs = {'plugin_files': [plugin]}
    def read():
        return proxy.execute_readonly_ability(site.id, 'kosmos-bridge/get-plugin-auto-update-policy', inputs)['result']['plugins'][0]
    before = read()
    assert before['installed'] and not before['blocked'] and not before['configured'], 'Test requires an installed plugin with no automatic updates or existing block.'
    try:
        result = proxy.execute_ability(site.id, 'kosmos-bridge/set-plugin-auto-update-policy', {**inputs, 'blocked': True})
        assert result['result']['plugins'][0]['verified']
        assert read()['blocked']
        print('Signed Bridge write and persisted readback: passed.')
    finally:
        restored = proxy.execute_ability(site.id, 'kosmos-bridge/set-plugin-auto-update-policy', {**inputs, 'blocked': False})
        assert restored['result']['plugins'][0]['verified']
        assert read() == before, 'Test policy restoration must match initial state.'
        db.commit()
        print('Original test-site policy restored; no automatic updates enabled.')
