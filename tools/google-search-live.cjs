// Read-only live QA. Google navigation is intercepted; no CRM data is sent to Google.
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
  const cookie = execFileSync('ssh', ['-o', 'ConnectTimeout=15', '-i', 'C:/Users/User/.ssh/kosmos_api_root_ed25519',
    'root@31.70.92.95', '/opt/kosmos-hub/venv/bin/python', '-'], {input: auth, encoding: 'utf8', timeout: 90000}).trim();
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    const context = await browser.newContext({viewport: {width: 1263, height: 912}, serviceWorkers: 'block'});
    await context.addCookies([{name: 'kosmos_hub_session', value: cookie, url: base, secure: true, httpOnly: true}]);
    let interceptedSearches = 0;
    await context.route('**/*', route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === 'https://www.google.com') {
        interceptedSearches++;
        return route.fulfill({status: 200, contentType: 'text/html', body: '<title>Intercepted test search</title>'});
      }
      if (url.origin === base && ['GET', 'HEAD'].includes(request.method())) return route.continue();
      return route.abort();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(45000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const pathname of ['/customers/1', '/leads/1803']) {
      for (const width of [1263, 390]) {
        await page.setViewportSize({width, height: 912});
        const response = await page.goto(base + pathname, {waitUntil: 'domcontentloaded'});
        assert.equal(response.status(), 200);
        const menu = page.locator('.detail-actions-menu').filter({has: page.getByRole('link', {name: 'Google Suche', exact: true})});
        await menu.locator('summary').click();
        const link = menu.getByRole('link', {name: 'Google Suche', exact: true});
        assert.equal(await link.isVisible(), true);
        const href = await link.getAttribute('href');
        const url = new URL(href);
        assert.equal(url.origin + url.pathname, 'https://www.google.com/search');
        assert.ok(url.searchParams.get('q').trim());
        assert.equal(await link.getAttribute('target'), '_blank');
        assert.deepEqual((await link.getAttribute('rel')).split(' ').sort(), ['noopener', 'noreferrer']);
        const bounds = await link.boundingBox();
        assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1);
        const popupPromise = context.waitForEvent('page');
        await link.click();
        const popup = await popupPromise;
        await popup.waitForLoadState('domcontentloaded');
        assert.equal(popup.url(), href);
        assert.equal(await popup.evaluate(() => window.opener), null);
        assert.equal(page.url(), base + pathname);
        await popup.close();
        console.log(JSON.stringify({path: pathname, width, menu: true, new_tab: true, external_request_intercepted: true}));
      }
    }
    assert.equal(interceptedSearches, 4);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error.message); process.exitCode = 1;});
