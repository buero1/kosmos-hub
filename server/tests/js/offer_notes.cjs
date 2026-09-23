const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const directory = path.resolve('tmp/offer-notes-browser');
const {record_id: id} = JSON.parse(fs.readFileSync(path.join(directory, 'data.json'), 'utf8'));
const html = name => fs.readFileSync(path.join(directory, name + '.html'), 'utf8');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      let fail = true, saved = false, submitted = '';
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (url.hostname !== 'hub.test') return route.abort();
        if (url.pathname === `/finance/offers/${id}/fields` && request.method() === 'POST') {
          submitted = request.postData();
          if (fail) return route.fulfill({status: 400, json: {detail: 'Position: Einzelpreis netto ist erforderlich.'}});
          saved = true;
          return route.fulfill({json: {redirect_url: `/finance/offers/${id}?fields=success`}});
        }
        if (url.pathname === '/finance/offers/new') return route.fulfill({contentType: 'text/html', body: html('create')});
        if (url.pathname === `/finance/offers/${id}`) return route.fulfill({contentType: 'text/html', body: html(saved ? 'saved' : 'original')});
        if (url.pathname.startsWith('/static/')) {
          const file = path.resolve('app', '.' + url.pathname);
          if (file.startsWith(path.resolve('app/static') + path.sep) && fs.existsSync(file))
            return route.fulfill({body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : 'text/css'});
          return route.fulfill({status: 404, body: ''});
        }
        return route.fulfill({json: {messages: [], contexts: [], status: 'idle'}});
      });
      const setHTML = value => page.evaluate(value => window.KosmosEmailEditors.setHTML(document.querySelector('[data-offer-notes] [data-email-compose-content]'), value), value);
      const getHTML = () => page.evaluate(() => window.KosmosEmailEditors.getHTML(document.querySelector('[data-offer-notes] [data-email-compose-content]')));
      await page.goto(`http://hub.test/finance/offers/${id}`);
      const panel = page.locator('#finance-offer-notes');
      assert.equal(await panel.locator('details').getAttribute('open'), null);
      await panel.locator('summary').click();
      assert.ok(await panel.locator('.finance-offer-notes-content').isVisible());
      await page.locator('[data-customer-edit-open]').click();
      assert.ok(await panel.locator('.jodit-container').isVisible());
      await setHTML('<p>Verwerfen</p>');
      await page.locator('[data-customer-edit-cancel]').click();
      await page.locator('[data-customer-edit-open]').click();
      await page.waitForFunction(() => window.KosmosEmailEditors.getHTML(document.querySelector('[data-offer-notes] [data-email-compose-content]')).includes('Original'));
      await setHTML('<p>Gespeicherte <strong>Anmerkung</strong>.</p>');
      const save = page.locator('button[type="submit"][form="finance-offer-fields-form"]');
      await save.click();
      await page.locator('.hub-form-error').waitFor();
      assert.match(submitted, /offer_field__notes/);
      assert.match(submitted, /Gespeicherte/);
      assert.match(await getHTML(), /Gespeicherte/);
      assert.ok(await save.isVisible());
      await panel.scrollIntoViewIfNeeded();
      await page.screenshot({path: path.join(directory, `edit-${width}.png`)});
      fail = false;
      await save.click();
      await page.waitForURL('**/*fields=success');
      assert.ok(await page.locator('[data-customer-edit-open]').isVisible());
      assert.equal(await panel.locator('details').getAttribute('open'), null);
      await panel.locator('summary').click();
      assert.match(await panel.locator('.finance-offer-notes-content').textContent(), /Gespeicherte/);
      assert.ok(!(await save.isVisible()));
      await page.goto('http://hub.test/finance/offers/new');
      await panel.locator('summary').click();
      assert.ok(await panel.locator('.jodit-container').isVisible());
      assert.match(await getHTML(), /Offer.ValidUntil/);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Offer notes desktop/mobile: collapsed read panel, rich editor, defaults, cancel reset, validation preservation and successful close OK.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
