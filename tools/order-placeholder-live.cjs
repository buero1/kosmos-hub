// Search and insert locally; saving templates and other write requests are blocked.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const base = 'https://kosmos-hub.31-70-92-95.sslip.io';
const auth = [
  'import os,sys,json,base64',
  'from dotenv import load_dotenv',
  'from itsdangerous import TimestampSigner',
  "os.chdir('/opt/kosmos-hub/app/server');sys.path.insert(0,os.getcwd())",
  "load_dotenv('/etc/kosmos-hub/kosmos-hub.env')",
  'from sqlalchemy import select,text',
  'from app.db.base import Base',
  'from app.db.session import SessionLocal',
  'from app.models.hub_user import HubUser',
  'from app.core.config import get_settings',
  'from app.services.template_placeholders import pdf_document_placeholders',
  'from app.services.hub_finance_document_field_catalog import ORDER_FIELDS',
  'with SessionLocal() as db:',
  " db.execute(text('SET TRANSACTION READ ONLY'))",
  " user=db.scalar(select(HubUser).where(HubUser.role=='admin',HubUser.is_active.is_(True)))",
  " data=base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())",
  " fields=[{'token':i.token,'label':i.label} for i in pdf_document_placeholders('orders') if i.profile_key]",
  ' assert len(fields)==len(ORDER_FIELDS)',
  " print(json.dumps({'cookie':TimestampSigner(get_settings().app_secret_key).sign(data).decode(),'fields':fields}))",
].join('\n');

(async () => {
  const {cookie, fields} = JSON.parse(execFileSync('ssh', ['-o', 'ConnectTimeout=15', '-i',
    'C:/Users/User/.ssh/kosmos_api_root_ed25519', 'root@31.70.92.95',
    '/opt/kosmos-hub/venv/bin/python', '-'], {input: auth, encoding: 'utf8', timeout: 90000}));
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  try {
    const context = await browser.newContext();
    await context.addCookies([{name: 'kosmos_hub_session', value: cookie, url: base, secure: true, httpOnly: true}]);
    let writes = 0;
    const results = [];
    for (const width of [1263, 390]) {
      const page = await context.newPage();
      await page.setViewportSize({width, height: 912});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.setDefaultTimeout(45000);
      await page.route('**/*', route => {
        if (!['GET', 'HEAD'].includes(route.request().method())) {writes++; return route.abort();}
        return route.continue();
      });
      const response = await page.goto(base + '/settings?pdf_template_type=orders#account-pdf-templates',
        {waitUntil: 'domcontentloaded'});
      assert.equal(response.status(), 200);
      await page.locator('[data-pdf-template-edit-block="payment"]').click();
      const drawer = page.locator('[data-pdf-template-edit-drawer]');
      await drawer.waitFor({state: 'visible'});
      await drawer.locator('[data-template-placeholder-source]').selectOption('Aktueller Auftrag');
      const search = drawer.locator('[data-template-placeholder-search]');
      const editor = drawer.frameLocator('.jodit-wysiwyg_iframe').locator('body');
      await editor.waitFor({state: 'visible'});
      for (const {label, token} of fields) {
        await search.fill(label);
        const option = drawer.locator(`[data-template-placeholder-option][data-token="${token}"]`);
        assert.equal(await option.count(), 1);
        assert.equal(await option.isVisible(), true, label);
        await option.click();
        await editor.evaluate((body, token) => {
          if (!body.textContent.includes(token)) throw new Error('Missing locally inserted token: ' + token);
        }, token);
      }
      await drawer.getByRole('button', {name: 'Abbrechen', exact: true}).click();
      await drawer.waitFor({state: 'hidden'});
      assert.deepEqual(errors, []);
      results.push({width, order_fields: fields.length, inserted_locally: true, cancelled: true});
      await page.close();
    }
    assert.equal(writes, 0);
    console.log(JSON.stringify({results, writes}));
  } finally {await browser.close();}
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
