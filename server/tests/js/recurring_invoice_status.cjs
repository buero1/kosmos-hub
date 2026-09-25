const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve('tmp/recurring-status-browser');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const initial of ['active', 'paused', 'ended']) {
      const html = fs.readFileSync(path.join(root, initial + '.html'), 'utf8');
      for (const width of [1263, 390]) {
        const page = await browser.newPage({viewport: {width, height: 912}});
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', async route => {
          const request = route.request(), url = new URL(request.url());
          assert.notEqual(request.method(), 'POST', 'Status test must not save real records');
          if (url.hostname !== 'hub.test') return route.abort();
          if (url.pathname === '/finance/recurring-invoices/1') return route.fulfill({contentType: 'text/html', body: html});
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
        const status = form.locator('[name="document_field__status"]');
        const nextDate = form.locator('[name="document_field__next_invoice_date"]');
        const contact = form.locator('[name="contact_id"]');
        const originalContact = await contact.inputValue();
        const initialDate = initial === 'active' ? '2026-09-25' : '';
        async function assertDate(value, active) {
          assert.equal(await nextDate.inputValue(), value);
          assert.equal(await nextDate.evaluate(input => input.required), active);
          assert.equal(await nextDate.evaluate(input => input.readOnly), !active);
          assert.equal(await nextDate.evaluate(input => input.closest('label').querySelector('small').hidden), !active);
          assert.equal(await form.evaluate(form => new FormData(form).get('document_field__next_invoice_date')), value);
        }
        await page.locator('[data-customer-edit-open]').click();
        await assertDate(initialDate, initial === 'active');
        for (const stopped of ['paused', 'ended']) {
          await status.selectOption('active');
          await nextDate.fill('2027-01-25');
          await status.selectOption(stopped);
          await assertDate('', false);
          assert.equal(await contact.inputValue(), originalContact);
          await status.selectOption('active');
          await assertDate('', true);
          assert.equal(await nextDate.evaluate(input => input.validity.valueMissing), true);
        }
        await nextDate.fill('2027-01-25');
        assert.equal(await nextDate.evaluate(input => input.checkValidity()), true);
        await status.selectOption('paused');
        await nextDate.scrollIntoViewIfNeeded();
        if (initial === 'active') await page.screenshot({path: path.join(root, `paused-${width}.png`)});
        await page.locator('[data-customer-edit-cancel]').click();
        await page.locator('[data-customer-edit-open]').click();
        assert.equal(await status.inputValue(), initial);
        await assertDate(initialDate, initial === 'active');
        assert.equal(await contact.inputValue(), originalContact);
        assert.deepEqual(errors, []);
        await page.close();
      }
    }
    console.log('Recurring status: clearing, reactivation validation, cancel/reset and contact preserved for 3 states at 1263/390px. No records saved.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
