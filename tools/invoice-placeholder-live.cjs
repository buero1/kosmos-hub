// Read-only live editor check: never save the locally inserted placeholders.
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
  'with SessionLocal() as db:',
  " db.execute(text('SET TRANSACTION READ ONLY'))",
  " user=db.scalar(select(HubUser).where(HubUser.role=='admin',HubUser.is_active.is_(True)))",
  " data=base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())",
  ' print(TimestampSigner(get_settings().app_secret_key).sign(data).decode())',
].join('\n');

(async () => {
  const cookie = execFileSync('ssh', ['-o', 'ConnectTimeout=15', '-i',
    'C:/Users/User/.ssh/kosmos_api_root_ed25519', 'root@31.70.92.95',
    '/opt/kosmos-hub/venv/bin/python', '-'], {input: auth, encoding: 'utf8', timeout: 90000}).trim();
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    const context = await browser.newContext();
    await context.addCookies([{name: 'kosmos_hub_session', value: cookie, url: base, secure: true, httpOnly: true}]);
    let writes = 0;
    const results = [];
    for (const width of [1263, 390]) {
      const page = await context.newPage();
      await page.setViewportSize({width, height: 912});
      page.setDefaultTimeout(45000);
      await page.route('**/*', route => {
        if (!['GET', 'HEAD'].includes(route.request().method())) {writes++; return route.abort();}
        return route.continue();
      });
      const response = await page.goto(base + '/settings?pdf_template_type=invoices#account-pdf-templates',
        {waitUntil: 'domcontentloaded'});
      assert.equal(response.status(), 200);
      await page.locator('[data-pdf-template-edit-block="payment"]').click();
      const drawer = page.locator('[data-pdf-template-edit-drawer]');
      await drawer.waitFor({state: 'visible'});
      await drawer.locator('[data-template-placeholder-source]').selectOption('Kunde');
      const search = drawer.locator('[data-template-placeholder-search]');
      const editor = drawer.frameLocator('.jodit-wysiwyg_iframe').locator('body');
      await editor.waitFor({state: 'visible'});
      for (const [label, token] of [['IBAN', '${Customer.Iban}'], ['BIC', '${Customer.Bic}'],
        ['Bankname', '${Customer.Bank}'], ['Kundennummer', '${Customer.CustomerNumber}']]) {
        await search.fill(label);
        const option = drawer.locator('[data-template-placeholder-option]').filter({hasText: token});
        assert.equal(await option.count(), 1);
        assert.equal(await option.isVisible(), true);
        await option.click();
        await page.waitForTimeout(100);
        assert.ok((await editor.textContent()).includes(token));
      }
      await drawer.getByRole('button', {name: 'Abbrechen', exact: true}).click();
      assert.equal(await drawer.isVisible(), false);
      results.push({width, customer_fields: 4, inserted_locally: true, cancelled: true});
      await page.close();
    }
    assert.equal(writes, 0);
    console.log(JSON.stringify({results, writes}));
  } finally {await browser.close();}
})().catch(error => {console.error(error.message); process.exitCode = 1;});
