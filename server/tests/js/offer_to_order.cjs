// Real templates and editor scripts, with no production requests or records.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve('tmp/offer-to-order-browser');
const data = JSON.parse(fs.readFileSync(path.join(root, 'data.json'), 'utf8'));
const html = name => fs.readFileSync(path.join(root, name + '.html'), 'utf8');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let conversions = 0, saves = 0;
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (url.hostname !== 'hub.test') return route.abort();
        if (url.pathname === `/finance/offers/${data.source}/convert-to-order` && request.method() === 'POST') {
          conversions++;
          return route.fulfill({json: {redirect_url: `/finance/orders/${data.order}?edit=true`}});
        }
        if (url.pathname === `/finance/orders/${data.order}/fields` && request.method() === 'POST') {
          saves++;
          return route.fulfill({status: 400, json: {detail: 'Test: Eingaben bleiben erhalten.'}});
        }
        if (url.pathname === `/finance/offers/${data.source}`) return route.fulfill({contentType: 'text/html', body: html('original')});
        if (url.pathname === `/finance/orders/${data.order}`) return route.fulfill({contentType: 'text/html', body: html('draft')});
        if (url.pathname.startsWith('/static/')) {
          const file = path.resolve('app', '.' + url.pathname);
          if (file.startsWith(path.resolve('app/static') + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
            return route.fulfill({body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : 'application/octet-stream'});
          }
        }
        return route.fulfill({json: {items: [], notifications: [], count: 0}});
      });
      await page.goto(`http://hub.test/finance/offers/${data.source}`);
      const menu = page.locator('.detail-actions-menu');
      await menu.locator('summary').click();
      await menu.getByRole('button', {name: 'In Auftrag umwandeln', exact: true}).click();
      await page.waitForURL(`**/finance/orders/${data.order}?edit=true`);
      const form = page.locator('#finance-document-fields-form');
      await form.waitFor({state: 'visible'});
      const positions = page.locator('[data-finance-document-position-editor]');
      assert.equal(await positions.isVisible(), true);
      assert.equal(await form.locator('[name="document_field__order_name"]').inputValue(), '');
      assert.equal(await form.locator('[name="document_field__order_date"]').inputValue(), '');
      assert.equal(await form.locator('[name="linked_record_id"]').inputValue(), String(data.source));
      assert.equal(await page.locator('[name="document_line__0__description"]').inputValue(), 'Original service description');
      assert.match(await positions.locator('[data-finance-document-total="total"]').textContent(), /CHF/);
      assert.equal(await form.evaluate(el => el.checkValidity()), false);
      await form.locator('[name="document_field__order_name"]').fill('Manually completed order');
      await form.locator('[name="document_field__order_date"]').fill('2026-09-25');
      await form.locator('[data-finance-customer-input]').fill('Finance test');
      await form.locator(`[data-finance-customer-option][data-customer-id="${data.customer}"]`).click();
      await form.locator('[name="contact_id"]').selectOption(String(data.contact));
      for (const key of ['contract_term', 'payment_method', 'payment_frequency', 'cancellation_period', 'order_intake_type']) {
        const select = form.locator(`[name="document_field__${key}"]`);
        await select.selectOption({index: 1});
      }
      assert.equal(await form.evaluate(el => el.checkValidity()), true);
      await page.locator('[data-customer-edit-actions] button[type="submit"]').click();
      await page.getByRole('alert').filter({hasText: 'Test: Eingaben bleiben erhalten.'}).waitFor();
      assert.equal(await form.isVisible(), true);
      assert.equal(await form.locator('[name="document_field__order_name"]').inputValue(), 'Manually completed order');
      assert.equal(conversions, 1);
      assert.equal(saves, 1);
      assert.deepEqual(errors, []);
      await page.screenshot({path: path.join(root, `order-${width}.png`), fullPage: false});
      await page.close();
    }
    console.log('Offer conversion: menu, edit mode, all positions, currency, required fields and failed-save preservation passed at 1263/390px.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
