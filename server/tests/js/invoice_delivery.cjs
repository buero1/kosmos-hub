// Rendered Hub templates, local fixtures and mocked transport only. No live mail.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve('tmp/invoice-delivery-browser');
const data = JSON.parse(fs.readFileSync(path.join(root, 'data.json'), 'utf8'));
const html = name => fs.readFileSync(path.join(root, name + '.html'), 'utf8');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let confirmed = 0;
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (url.hostname !== 'hub.test') return route.abort();
        if (url.pathname === '/finance/invoices/email-review') {
          assert.equal(request.method(), 'POST');
          return route.fulfill({json: {template: 'Rechnungen senden', sender: 'sender@example.test', review_token: 'test', rows: [
            {invoice_id: data.invoice, number: 'RE-TEST', customer: 'Finance test', contact: 'Test contact', email: 'test@example.test', pdf: 'test.pdf', issue: ''},
          ]}});
        }
        if (url.pathname === '/finance/invoices/email-confirm') {
          confirmed++;
          return route.fulfill({json: {batch_id: data.batch}});
        }
        if (url.pathname === `/finance/invoices/email-batches/${data.batch}`) return route.fulfill({json: data.result});
        if (url.pathname === '/finance/invoices') return route.fulfill({contentType: 'text/html', body: html(confirmed ? 'after' : 'before')});
        if (url.pathname === `/finance/invoices/${data.invoice}`) return route.fulfill({contentType: 'text/html', body: html('detail')});
        if (url.pathname.startsWith('/static/')) {
          const file = path.resolve('app', '.' + url.pathname);
          if (file.startsWith(path.resolve('app/static') + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
            const type = {'.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml'}[path.extname(file)];
            return route.fulfill({body: fs.readFileSync(file), contentType: type || 'application/octet-stream'});
          }
        }
        return route.fulfill({json: {items: [], notifications: [], count: 0}});
      });
      await page.goto('http://hub.test/finance/invoices');
      const status = page.locator(`[data-invoice-delivery-status="${data.invoice}"]`);
      const date = page.locator(`[data-invoice-delivery-date="${data.invoice}"]`);
      assert.equal(await status.textContent(), 'Noch nicht versendet');
      assert.equal(await date.textContent(), '-');
      const businessStatus = await status.locator('..').locator('td').nth(2).textContent();
      await page.locator(`[data-invoice-select="${data.invoice}"]`).check();
      await page.locator('[data-invoice-email-review-open]').click();
      await page.locator('[data-invoice-email-confirm]').click();
      await page.waitForFunction(id => document.querySelector(`[data-invoice-delivery-status="${id}"]`).textContent === 'Versendet', data.invoice);
      await page.locator('[data-invoice-email-close]').first().click();
      assert.equal(confirmed, 1);
      assert.equal(await date.textContent(), '25.09.2026 11:34');
      assert.equal(await status.locator('..').locator('td').nth(2).textContent(), businessStatus);
      assert.equal(await page.locator(`[data-invoice-select="${data.invoice}"]`).isChecked(), false);
      await page.reload();
      assert.equal(await status.textContent(), 'Versendet');
      assert.equal(await date.textContent(), '25.09.2026 11:34');
      await status.scrollIntoViewIfNeeded();
      await page.screenshot({path: path.join(root, `list-${width}.png`)});
      await page.goto(`http://hub.test/finance/invoices/${data.invoice}`);
      const panel = page.locator('[data-invoice-email-delivery]');
      await panel.scrollIntoViewIfNeeded();
      assert.deepEqual(await panel.locator('dt').allTextContents(), ['Versandstatus', 'Versendet am']);
      assert.deepEqual(await panel.locator('dd').allTextContents(), ['Versendet', '25.09.2026 11:34']);
      assert.equal(await panel.locator('input, select, a').count(), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({path: path.join(root, `detail-${width}.png`)});
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Invoice delivery: live column refresh, persistence, unchanged invoice status and detail sidebar passed at 1263/390px.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
