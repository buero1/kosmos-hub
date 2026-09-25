// Read-only live header check. Never prepares a draft or sends email.
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
  'from app.models.hub_finance_documents import HubFinanceOrder',
  'from app.core.config import get_settings',
  'with SessionLocal() as db:',
  " db.execute(text('SET TRANSACTION READ ONLY'))",
  " user=db.scalar(select(HubUser).where(HubUser.role=='admin',HubUser.is_active.is_(True)))",
  " order=db.scalar(select(HubFinanceOrder.id).where(HubFinanceOrder.customer_id.is_not(None)).order_by(HubFinanceOrder.id.desc()))",
  " data=base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())",
  " print(json.dumps({'cookie':TimestampSigner(get_settings().app_secret_key).sign(data).decode(),'order':order}))",
].join('\n');

(async () => {
  const {cookie, order} = JSON.parse(execFileSync('ssh', ['-o', 'ConnectTimeout=15', '-i',
    'C:/Users/User/.ssh/kosmos_api_root_ed25519', 'root@31.70.92.95',
    '/opt/kosmos-hub/venv/bin/python', '-'], {input: auth, encoding: 'utf8', timeout: 90000}));
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  try {
    const context = await browser.newContext();
    await context.addCookies([{name: 'kosmos_hub_session', value: cookie, url: base, secure: true, httpOnly: true}]);
    let writes = 0;
    for (const width of [1263, 390]) {
      const page = await context.newPage();
      await page.setViewportSize({width, height: 912});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => {
        if (!['GET', 'HEAD'].includes(route.request().method())) {writes++; return route.abort();}
        return route.continue();
      });
      const response = await page.goto(`${base}/finance/orders/${order}`, {waitUntil: 'domcontentloaded'});
      assert.equal(response.status(), 200);
      const button = page.locator(`[data-order-email-compose="${order}"]`);
      assert.equal(await button.count(), 1);
      assert.equal(await button.isVisible(), true);
      assert.equal(await button.innerText(), 'Per E-Mail versenden');
      await page.getByRole('button', {name: 'Bearbeiten', exact: true}).click();
      assert.equal(await button.isVisible(), false);
      assert.deepEqual(errors, []);
      await page.close();
    }
    assert.equal(writes, 0);
    console.log(JSON.stringify({order, widths: [1263, 390], header_button: true, hidden_while_editing: true, writes}));
  } finally {await browser.close();}
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
