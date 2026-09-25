// Rendered Hub templates + real editor JavaScript, isolated from production data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const directory = path.resolve('tmp/offer-duplicate-browser');
const data = JSON.parse(fs.readFileSync(path.join(directory, 'data.json'), 'utf8'));
const html = name => fs.readFileSync(path.join(directory, name + '.html'), 'utf8');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      let duplicates = 0;
      let discards = 0;
      let failedDiscards = 0;
      let saved = false;
      let failSave = true;
      await page.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        if (url.hostname !== 'hub.test') return route.abort();
        if (url.pathname === `/finance/offers/${data.source}/duplicate` && request.method() === 'POST') {
          duplicates += 1;
          return route.fulfill({json: {redirect_url: `/finance/offers/${data.copy}?edit=true`}});
        }
        if (url.pathname === `/finance/offers/${data.copy}/discard-copy` && request.method() === 'POST') {
          assert.equal(saved, false, 'Saved offers must never submit the discard form');
          assert.ok(!request.postData().includes('offer_field__'), 'Discard must not validate or submit the field editor');
          if (failedDiscards++ === 0) return route.fulfill({status: 500, json: {detail: 'Test: Abbrechen fehlgeschlagen.'}});
          discards += 1;
          return route.fulfill({json: {redirect_url: `/finance/offers/${data.source}`}});
        }
        if (url.pathname === `/finance/offers/${data.copy}/fields` && request.method() === 'POST') {
          if (failSave) return route.fulfill({status: 400, json: {detail: 'Test: Pflichtfeld fehlt.'}});
          saved = true;
          return route.fulfill({json: {redirect_url: `/finance/offers/${data.copy}?fields=success#finance-offer-fields`}});
        }
        if (url.pathname === `/finance/offers/${data.source}`) return route.fulfill({contentType: 'text/html', body: html('original')});
        if (url.pathname === `/finance/offers/${data.copy}`) return route.fulfill({contentType: 'text/html', body: html(saved ? 'saved' : 'duplicate')});
        if (url.pathname.startsWith('/static/')) {
          const file = path.resolve('app', '.' + url.pathname);
          const root = path.resolve('app/static') + path.sep;
          if (file.startsWith(root) && fs.existsSync(file) && fs.statSync(file).isFile()) {
            return route.fulfill({body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : 'application/octet-stream'});
          }
          return route.fulfill({status: 404, body: ''});
        }
        return route.fulfill({json: {messages: [], contexts: [], status: 'idle'}});
      });
      await page.goto(`http://hub.test/finance/offers/${data.source}`);
      const menu = page.locator('[data-customer-actions-menu]');
      await menu.locator('summary').click();
      assert.equal(await menu.locator('button').first().textContent(), 'Duplizieren');
      await menu.getByRole('button', {name: 'Duplizieren', exact: true}).click();
      const form = page.locator('#finance-offer-fields-form');
      await form.waitFor({state: 'visible'});
      assert.equal(duplicates, 1);
      assert.equal(page.url(), `http://hub.test/finance/offers/${data.copy}`);
      assert.equal(await form.locator('[name="customer_id"]').inputValue(), '');
      assert.equal(await form.locator('[name="lead_id"]').inputValue(), '');
      assert.equal(await form.locator('[name="contact_id"]').inputValue(), '');
      assert.equal(await form.locator('[name="offer_field__status"]').inputValue(), 'draft');
      assert.equal(await page.locator('[data-finance-position-editor]').isVisible(), true);
      assert.equal(await page.locator('article[data-customer-edit-position-readonly]').isVisible(), false);
      assert.equal(await page.locator('[data-customer-edit-actions]').isVisible(), true);
      assert.equal(await page.locator('[data-customer-edit-open]').isVisible(), false);
      await page.screenshot({path: path.join(directory, `edit-${width}.png`), fullPage: false});
      await form.locator('[name="offer_field__reference"]').fill('Discard only after confirmation');
      const saveButton = page.locator('[data-customer-edit-actions] button[form="finance-offer-fields-form"]');
      await saveButton.click();
      assert.equal(await form.isVisible(), true, 'Missing customer/lead must keep the editor open');
      assert.equal(await form.locator('[data-finance-party-input]').evaluate(el => el.checkValidity()), false);
      await page.locator('[data-customer-edit-cancel]').click();
      await page.getByRole('alert').filter({hasText: 'Test: Abbrechen fehlgeschlagen.'}).waitFor();
      assert.equal(await form.isVisible(), true, 'Failed discard must not close/reset the editor');
      assert.equal(await form.locator('[name="offer_field__reference"]').inputValue(), 'Discard only after confirmation');
      await page.locator('[data-customer-edit-cancel]').click();
      await page.waitForURL(`**/finance/offers/${data.source}`);
      await form.waitFor({state: 'hidden'});
      assert.equal(discards, 1);
      await menu.locator('summary').click();
      await menu.getByRole('button', {name: 'Duplizieren', exact: true}).click();
      await form.waitFor({state: 'visible'});
      await form.locator('[data-finance-party-input]').fill('Finance test');
      await form.locator(`[data-party-type="customer"][data-party-id="${data.customer}"]`).click();
      await form.locator('[name="contact_id"]').selectOption(String(data.contact));
      await form.locator('[name="offer_field__reference"]').fill('Unsaved change');
      await saveButton.click();
      await page.locator('.hub-form-error:visible').waitFor();
      assert.equal(await form.isVisible(), true);
      assert.equal(await form.locator('[name="offer_field__reference"]').inputValue(), 'Unsaved change');
      // The reported server-side validation error must also permit discarding.
      await page.locator('[data-customer-edit-cancel]').click();
      await page.waitForURL(`**/finance/offers/${data.source}`);
      assert.equal(discards, 2);
      await menu.locator('summary').click();
      await menu.getByRole('button', {name: 'Duplizieren', exact: true}).click();
      await form.waitFor({state: 'visible'});
      await form.locator('[data-finance-party-input]').fill('Finance test');
      await form.locator(`[data-party-type="customer"][data-party-id="${data.customer}"]`).click();
      await form.locator('[name="contact_id"]').selectOption(String(data.contact));
      failSave = false;
      await saveButton.click();
      await page.waitForURL(`**/finance/offers/${data.copy}?fields=success#finance-offer-fields`);
      await page.waitForLoadState('load');
      await form.waitFor({state: 'hidden'});
      assert.equal(await page.locator('[data-customer-edit-actions]').isVisible(), false);
      assert.equal(await page.locator('[data-customer-edit-open]').isVisible(), true);
      await page.locator('[data-customer-edit-open]').click();
      await form.locator('[name="offer_field__reference"]').fill('Normal edit cancelled');
      await page.locator('[data-customer-edit-cancel]').click();
      await form.waitFor({state: 'hidden'});
      assert.equal(discards, 2, 'Normal cancel must not discard a successfully saved offer');
      assert.equal(page.url().includes(`/finance/offers/${data.copy}?fields=success`), true);
      assert.equal(duplicates, 3, 'Only explicit duplicate clicks create copies');
      await page.close();
    }
    console.log('Browser OK at desktop/mobile: cancel discards after client/server validation errors; failed discard preserves input; successful save exits editor; normal cancel never discards saved offers.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
