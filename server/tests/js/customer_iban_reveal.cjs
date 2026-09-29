// Synthetic bank value and local HTML only; never fetch a real customer IBAN.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const iban = 'DE89370400440532013000';
const render = `
import re
from pathlib import Path
from types import SimpleNamespace as S
from jinja2 import Environment
source=Path('app/templates/customer_detail.html').read_text(encoding='utf-8')
names=('customer_iban_reveal','customer_profile_field_value','customer_field_control')
macros='\\n'.join(re.search(r'{% macro '+n+r'\\(.*?{% endmacro %}', source, re.S)[0] for n in names)
field=S(key='iban',label='IBAN',value='Geschuetzt',sensitive=True,url_href=None,form_value='',options=(),display_type='Einzelzeile')
body=Environment(autoescape=True).from_string(macros+'''<h1>Bankverbindung</h1><form><section>{{ customer_profile_field_value(field) }}</section><section>{{ customer_field_control(field, 'customer_field__iban') }}</section><input name="other" value="unchanged"></form>''').render(field=field,detail=S(entry=S(customer=S(id=411))),csrf_token='test-csrf',request=S(state=S(hub_user=S(role='admin',is_active=True))))
print('<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/customer-iban.css"><style>body{max-width:600px;margin:24px;font:16px sans-serif}section{padding:16px;border:1px solid #ddd;margin:16px 0}button{cursor:pointer}label{display:grid;gap:8px}input{max-width:100%;box-sizing:border-box}</style>'+body+'<script src="/customer-iban.js"></script>')
`;

(async () => {
  const html = execFileSync('.venv/Scripts/python.exe', ['-c', render], {encoding: 'utf8'});
  const server = http.createServer((request, response) => {
    assert.equal(request.method, 'GET', 'Every POST must be intercepted in the test');
    const name = request.url === '/customer-iban.js' ? 'customer-iban.js' : request.url === '/customer-iban.css' ? 'customer-iban.css' : null;
    response.setHeader('Content-Type', name ? (name.endsWith('js') ? 'application/javascript' : 'text/css') : 'text/html');
    response.end(name ? fs.readFileSync('app/static/' + name) : html);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, channel: 'chrome'});
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      await page.clock.install();
      let requests = 0, responseMode = 'ok', release;
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.addInitScript(() => {
        window.copiedTestValue = null;
        Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => { window.copiedTestValue = value; }}});
      });
      await page.route('**/iban/reveal', async route => {
        requests++;
        assert.equal(route.request().method(), 'POST');
        assert.equal(route.request().postData(), 'csrf_token=test-csrf');
        if (responseMode === 'late') await new Promise(resolve => { release = resolve; });
        await route.fulfill({status: responseMode === 'denied' ? 403 : 200, contentType: 'application/json',
          body: JSON.stringify(responseMode === 'denied' ? {detail: 'Nur Superadmins duerfen die IBAN anzeigen.'} : {iban})}).catch(() => {});
      });
      await page.goto('http://127.0.0.1:' + server.address().port);
      const first = page.locator('[data-iban-reveal]').first();
      const second = page.locator('[data-iban-reveal]').nth(1);
      const reveal = async widget => {
        await widget.locator('[data-iban-show]').click();
        await widget.locator('[data-iban-value]:not([hidden])').waitFor();
        assert.equal(await widget.locator('[data-iban-value]').textContent(), iban);
      };
      const cleared = async () => {
        assert.equal(await page.locator('[data-iban-value]').allTextContents().then(values => values.join('')), '');
        assert.equal(await page.locator('[data-iban-copy]:not([hidden])').count(), 0);
      };
      assert.equal(requests, 0);
      assert.ok(!(await page.content()).includes(iban));
      await reveal(first);
      await first.locator('[data-iban-copy]').click();
      assert.equal(await page.evaluate(() => window.copiedTestValue), iban);
      assert.equal(await page.locator('input[name="customer_field__iban"]').inputValue(), '');
      assert.ok(!(await page.evaluate(() => new URLSearchParams(new FormData(document.querySelector('form'))).toString())).includes(iban));
      await page.clock.runFor(30001);
      await cleared();
      await reveal(first);
      await reveal(second);
      assert.equal(await first.locator('[data-iban-value]').textContent(), '');
      await page.keyboard.press('Escape');
      await cleared();
      await reveal(first);
      await page.evaluate(() => {
        Object.defineProperty(document, 'hidden', {value: true, configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await cleared();
      await page.evaluate(() => { delete document.hidden; });
      responseMode = 'denied';
      await first.locator('[data-iban-show]').click();
      await first.locator('[data-iban-status]').filter({hasText: 'Nur Superadmins'}).waitFor();
      await cleared();
      responseMode = 'late';
      const pending = page.waitForRequest('**/iban/reveal');
      await first.locator('[data-iban-show]').click();
      await pending;
      await page.locator('h1').click();
      release();
      await page.waitForTimeout(150);
      await cleared();
      responseMode = 'ok';
      await reveal(first);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      if (process.env.IBAN_REVEAL_SCREENSHOTS) {
        await page.screenshot({path: '../tmp/customer-iban-' + width + '.png', fullPage: true});
      }
      await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
      await cleared();
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Desktop/mobile IBAN reveal, copy, expiry, errors, cancellation and form isolation passed.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
