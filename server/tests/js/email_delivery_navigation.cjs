// Local browser fixture only: no real mailbox or send endpoint is contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {chromium} = require('playwright');

(async () => {
  const helper = fs.readFileSync('app/static/email-delivery.js', 'utf8');
  let documents = 0;
  const server = http.createServer((request, response) => {
    assert.equal(request.method, 'GET');
    if (request.url === '/email-delivery.js') {
      response.writeHead(200, {'Content-Type': 'application/javascript'});
      return response.end(helper);
    }
    if (request.url === '/favicon.ico') {
      response.writeHead(204);
      return response.end();
    }
    documents++;
    response.writeHead(200, {'Content-Type': 'text/html', 'Cache-Control': 'no-store'});
    response.end('<!doctype html><script src="/email-delivery.js"></script><p>Confirmed delivery</p>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, channel: 'chrome'});
    const page = await browser.newPage();
    const base = 'http://127.0.0.1:' + server.address().port;
    const target = '/customers/411?communication=success&message=sent#customer-communications';
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + target);
    let before = documents;
    await page.evaluate(url => window.location.assign(url), target);
    await page.waitForTimeout(250);
    assert.equal(documents, before, 'Reproduce old fragment-only navigation: no refreshed page');
    for (const current of [target, target.split('#')[0], target.replace('#customer-communications', '#customer-fields'), '/customers/412']) {
      await page.goto(base + current);
      before = documents;
      const loaded = page.waitForEvent('load');
      await page.evaluate(url => window.KosmosEmailDelivery.navigate(url), target);
      await loaded;
      assert.equal(documents, before + 1, 'Exactly one new GET, never a duplicate send');
      assert.equal(page.url(), base + target);
    }
    assert.deepEqual(errors, []);
    console.log('Old stuck state reproduced; all four fixed navigation cases passed. No emails sent.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
