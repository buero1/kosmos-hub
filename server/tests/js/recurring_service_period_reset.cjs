const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.resolve('tmp/recurring-status-browser');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    const html = fs.readFileSync(path.join(root, 'active.html'), 'utf8');
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        assert.notEqual(request.method(), 'POST', 'Reset test must not save records');
        if (url.hostname !== 'hub.test') return route.abort();
        if (url.pathname.startsWith('/finance/recurring-invoices/')) {
          if (url.pathname.endsWith('/next-invoice-preview')) {
            return route.fulfill({contentType: 'application/pdf', body: Buffer.from('%PDF-1.4\n%%EOF')});
          }
          return route.fulfill({contentType: 'text/html', body: html});
        }
        if (url.pathname.startsWith('/static/')) {
          const file = path.resolve('app', '.' + url.pathname);
          if (file.startsWith(path.resolve('app/static') + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
            return route.fulfill({body: fs.readFileSync(file), contentType: {
              '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml',
            }[path.extname(file)] || 'application/octet-stream'});
          }
        }
        return route.fulfill({json: {items: [], notifications: [], recipients: [], count: 0}});
      });
      await page.goto('http://hub.test/finance/recurring-invoices/1');

      const form = page.locator('[data-customer-field-editor]');
      const nextDate = form.locator('[name="document_field__next_invoice_date"]');
      const serviceStart = form.locator('[name="document_field__service_period_start"]');
      const serviceEnd = form.locator('[name="document_field__service_period_end"]');
      async function assertPeriod(start, end) {
        assert.equal(await serviceStart.inputValue(), start);
        assert.equal(await serviceEnd.inputValue(), end);
      }

      await page.locator('[data-customer-edit-open]').click();
      await assertPeriod('2026-10-01', '2026-10-31');
      await nextDate.fill('2027-01-25');
      await nextDate.dispatchEvent('change');
      await assertPeriod('2027-02-01', '2027-02-28');

      await page.locator('[data-customer-edit-cancel]').click();
      await page.locator('[data-customer-edit-open]').click();
      await assertPeriod('2026-10-01', '2026-10-31');
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Recurring service period is restored after cancel and reopen at 1263/390px. No records saved.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
