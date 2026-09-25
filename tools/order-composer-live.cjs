// By default read-only. --prepare opens real drafts and removes only those drafts.
// All other browser writes, especially sending and autosave, are blocked.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const base = 'https://kosmos-hub.31-70-92-95.sslip.io';
const prepare = process.argv.includes('--prepare');
const orderArg = process.argv.find(arg => arg.startsWith('--order='));
const requestedOrder = orderArg ? Number(orderArg.split('=')[1]) : null;
assert.ok(requestedOrder === null || (Number.isSafeInteger(requestedOrder) && requestedOrder > 0));
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
  ` order=${requestedOrder || "db.scalar(select(HubFinanceOrder.id).where(HubFinanceOrder.customer_id.is_not(None)).order_by(HubFinanceOrder.id.desc()))"}`,
  " data=base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())",
  " print(json.dumps({'cookie':TimestampSigner(get_settings().app_secret_key).sign(data).decode(),'order':order,'user_id':user.id}))",
].join('\n');

(async () => {
  const sshArgs = ['-o', 'ConnectTimeout=15', '-i',
    'C:/Users/User/.ssh/kosmos_api_root_ed25519', 'root@31.70.92.95',
    '/opt/kosmos-hub/venv/bin/python', '-'];
  const {cookie, order, user_id} = JSON.parse(execFileSync('ssh', sshArgs, {input: auth, encoding: 'utf8', timeout: 90000}));
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  const drafts = [];
  try {
    const context = await browser.newContext();
    await context.addCookies([{name: 'kosmos_hub_session', value: cookie, url: base, secure: true, httpOnly: true}]);
    let writes = 0;
    for (const width of [1263, 390]) {
      const page = await context.newPage();
      await page.setViewportSize({width, height: 912});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let prepareCalls = 0;
      await page.route('**/*', route => {
        if (prepare && new URL(route.request().url()).pathname === `/finance/orders/${order}/email-compose`
            && route.request().method() === 'POST' && prepareCalls++ === 0) return route.continue();
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
      await page.locator('[data-customer-edit-cancel]').click();
      if (prepare) {
        const responsePromise = page.waitForResponse(response => response.url().endsWith(`/finance/orders/${order}/email-compose`)
          && response.request().method() === 'POST');
        await button.click();
        const prepared = await responsePromise;
        const payload = await prepared.json();
        assert.equal(prepared.status(), 200);
        assert.ok(Number.isSafeInteger(payload.draft_id));
        drafts.push(payload.draft_id);
        const drawer = page.locator('[data-global-mailbox-composer]');
        await drawer.locator('[data-email-compose-attachment-list] a').waitFor({state: 'visible'});
        assert.ok(payload.template_id && payload.recipient_email && payload.customer_id);
        assert.equal(payload.order_id, order);
        assert.equal(await drawer.locator('[data-global-mailbox-template-search]').inputValue(), 'Auftragsbestätigung');
        assert.equal(await drawer.locator('[name="recipient_email"]').inputValue(), payload.recipient_email);
        assert.equal(await drawer.locator('[name="recipient_customer_id"]').inputValue(), String(payload.customer_id));
        assert.equal(await drawer.locator('[name="subject"]').inputValue(), payload.subject);
        assert.equal(await drawer.locator('[data-email-compose-attachment-list] a').count(), payload.attachments.length);
        assert.ok(payload.attachments.some(file => file.filename.toLowerCase().endsWith('.pdf')));
        assert.equal(await drawer.locator('[name="scheduled_at"]').isVisible(), false);
      }
      assert.deepEqual(errors, []);
      await page.close();
    }
    assert.equal(writes, 0);
    console.log(JSON.stringify({order, widths: [1263, 390], header_button: true, real_preparation: prepare,
      drafts_created: drafts.length, template_and_pdf: prepare, blocked_writes: writes, sent: 0}));
  } finally {
    await browser.close();
    if (drafts.length) {
      const cleanup = [
        ...auth.slice(0, auth.indexOf('with SessionLocal() as db:')).split('\n'),
        'from app.core.security import get_secret_cipher',
        'from app.models.hub_mailbox_email import HubMailboxEmail',
        'from app.services.hub_mailbox import HubMailboxService',
        'with SessionLocal() as db:',
        ` user=db.get(HubUser, ${user_id})`,
        " box=HubMailboxService(db=db,cipher=get_secret_cipher(),actor=user.username,public_base_url=get_settings().public_base_url)",
        ` for draft_id in ${JSON.stringify(drafts)}:`,
        '  row=db.get(HubMailboxEmail,draft_id)',
        "  assert row and row.source=='hub-draft' and row.mailbox_state=='draft'",
        '  payload=box._payload(row.encrypted_payload_json)',
        `  assert payload['order_dispatch']['order_id']==${order} and payload['order_dispatch']['state']=='ready'`,
        '  assert box.discard_draft(draft_id=draft_id)',
        ' db.commit()',
        ` print('Removed ${drafts.length} test drafts; no emails sent.')`,
      ].join('\n');
      console.log(execFileSync('ssh', sshArgs, {input: cleanup, encoding: 'utf8', timeout: 90000}).trim());
    }
  }
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
