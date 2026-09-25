const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve('tmp/finance-relations-browser');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const kind of ['recurring-invoices', 'invoices', 'orders', 'dunnings', 'offers']) {
      const data = JSON.parse(fs.readFileSync(path.join(root, kind + '.json'), 'utf8'));
      let html = fs.readFileSync(path.join(root, kind + '.html'), 'utf8');
      // This partial has no template variables; exercise current JS with the rendered fixture.
      const component = html.indexOf("document.querySelectorAll('[data-finance-customer-search]')");
      assert.ok(component >= 0);
      const start = html.lastIndexOf('<script>', component), end = html.indexOf('</script>', component) + 9;
      html = html.slice(0, start) + fs.readFileSync('app/templates/partials/finance_customer_search_script.html', 'utf8') + html.slice(end);
      for (const width of [1263, 390]) {
        const page = await browser.newPage({viewport: {width, height: 912}});
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', async route => {
          const request = route.request(), url = new URL(request.url());
          assert.notEqual(request.method(), 'POST', 'This browser test must not save records');
          if (url.hostname !== 'hub.test') return route.abort();
          if (url.pathname === `/finance/${kind}/${data.id}`) return route.fulfill({contentType: 'text/html', body: html});
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
        await page.goto(`http://hub.test/finance/${kind}/${data.id}`);
        const form = page.locator('[data-customer-field-editor]');
        const customer = form.locator('[data-finance-customer-input], [data-finance-party-input]');
        const contact = form.locator('[data-finance-contact]');
        async function assertRelations() {
          assert.equal(await customer.isVisible(), true);
          assert.ok((await customer.inputValue()).includes(data.customer_name));
          assert.equal(await contact.isVisible(), true);
          assert.equal(await contact.inputValue(), String(data.contact));
          assert.equal(await contact.locator('option:checked').isDisabled(), false);
          const payload = await form.evaluate(form => Object.fromEntries(new FormData(form)));
          assert.equal(payload.customer_id, String(data.customer));
          assert.equal(payload.contact_id, String(data.contact));
        }
        await page.locator('[data-customer-edit-open]').click();
        await assertRelations();
        if (kind === 'recurring-invoices') {
          await customer.scrollIntoViewIfNeeded();
          await page.screenshot({path: path.join(root, `recurring-edit-${width}.png`)});
        }
        await customer.fill('No matching customer');
        assert.equal(await contact.inputValue(), '');
        await page.locator('[data-customer-edit-cancel]').click();
        await page.locator('[data-customer-edit-open]').click();
        await assertRelations();
        assert.deepEqual(errors, []);
        await page.close();
      }
    }
    console.log('Existing customer/contact preserved in edit and cancel/reopen across five Finance modules at 1263/390px. No records saved.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
