// Exercise the live UI without confirming or transmitting a policy change.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = 'https://kosmos-hub.31-70-92-95.sslip.io';
const auth = `
import os,sys,json,base64
from dotenv import load_dotenv
from itsdangerous import TimestampSigner
os.chdir('/opt/kosmos-hub/app/server');sys.path.insert(0,os.getcwd())
load_dotenv('/etc/kosmos-hub/kosmos-hub.env')
from sqlalchemy import select,text
from app.db.base import Base
from app.db.session import SessionLocal
from app.models.hub_user import HubUser
from app.core.config import get_settings
with SessionLocal() as db:
 db.execute(text('SET TRANSACTION READ ONLY'))
 user=db.scalar(select(HubUser).where(HubUser.role=='admin',HubUser.is_active.is_(True)))
 data=base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())
 print(TimestampSigner(get_settings().app_secret_key).sign(data).decode())
`;
(async () => {
  const cookie = execFileSync('ssh', ['-i', 'C:/Users/User/.ssh/kosmos_api_root_ed25519',
    'root@31.70.92.95', '/opt/kosmos-hub/venv/bin/python', '-'], {input: auth, encoding: 'utf8'}).trim();
  const browser = await chromium.launch({channel: 'chrome'});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 960}});
    await context.addCookies([{name: 'kosmos_hub_session', value: cookie, url: base, secure: true, httpOnly: true}]);
    const page = await context.newPage();
    let writes = 0;
    await page.route('**/updates/plugin-auto-updates', route => { writes++; return route.abort(); });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/updates?site_scope=selected&site_id=3', {waitUntil: 'domcontentloaded'});
    await page.locator('[data-update-action-select]').selectOption('auto-updates');
    await page.locator('input[name="plugin_file"][value="elementor/elementor.php"]').check();
    await page.locator('input[name="plugin_file"][value="elementor-pro/elementor-pro.php"]').check();
    assert.equal(await page.locator('[data-update-action-start]').isEnabled(), true);
    await page.locator('[data-update-action-start]').click();
    const dialog = page.locator('[data-auto-update-confirmation]');
    assert.equal(await dialog.isVisible(), true);
    assert.match(await dialog.innerText(), /Elementor Pro/);
    const out = path.resolve(__dirname, '../outputs');
    fs.mkdirSync(out, {recursive: true});
    await page.screenshot({path: path.join(out, 'plugin-policy-confirmation.png')});
    await page.locator('[data-auto-update-cancel]').click();
    assert.equal(await dialog.isVisible(), false);
    assert.equal(writes, 0);
    assert.deepEqual(errors, []);
    console.log('Live workbench: Elementor/Pro selection and confirmation rendered; cancellation made zero policy requests.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
