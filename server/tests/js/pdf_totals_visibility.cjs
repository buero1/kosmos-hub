const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const directory = path.resolve('tmp/pdf-totals-browser');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const width of [1263, 390]) {
      for (const kind of ['offers', 'orders', 'invoices', 'dunnings']) {
        const page = await browser.newPage({viewport: {width, height: 912}});
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        let enabled = false, submissions = 0;
        await page.route('**/*', async route => {
          const request = route.request(), url = new URL(request.url());
          if (url.hostname !== 'hub.test') return route.abort();
          if (/\/account\/pdf-templates\/\d+\/positions$/.test(url.pathname)) {
            assert.equal(request.method(), 'POST');
            const data = await new Response(request.postDataBuffer(), {headers: {'content-type': request.headers()['content-type']}}).formData();
            assert.equal(data.getAll('column_key').length, 9);
            enabled = data.get('show_totals') === 'true';
            submissions++;
            return route.fulfill({json: {redirect_url: `/settings?pdf_template_type=${kind}#account-pdf-templates`}});
          }
          if (url.pathname === '/settings') return route.fulfill({contentType: 'text/html',
            body: fs.readFileSync(path.join(directory, `${kind}-${enabled ? 'on' : 'off'}.html`), 'utf8')});
          if (url.pathname.startsWith('/static/')) {
            const file = path.resolve('app', '.' + url.pathname);
            if (file.startsWith(path.resolve('app/static') + path.sep) && fs.existsSync(file))
              return route.fulfill({body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : 'text/css'});
            return route.fulfill({status: 404, body: ''});
          }
          return route.fulfill({json: {messages: [], contexts: [], status: 'idle'}});
        });
        await page.goto(`http://hub.test/settings?pdf_template_type=${kind}#account-pdf-templates`);
        const totals = page.locator('.pdf-template-preview-totals');
        assert.equal(await totals.count(), 0);
        for (const desired of [true, false]) {
          await page.locator('[data-pdf-template-open-positions]').click();
          const form = page.locator('[data-pdf-template-position-form]');
          const checkbox = form.locator('[name="show_totals"]');
          assert.equal(await checkbox.isChecked(), !desired);
          await checkbox.setChecked(desired);
          if (kind === 'offers' && desired) await page.screenshot({path: path.join(directory, `editor-${width}.png`)});
          const navigation = page.waitForNavigation();
          await form.locator('button[type="submit"]').click();
          await navigation;
          await page.waitForFunction(desired =>
            Boolean(document.querySelector('.pdf-template-preview-totals')) === desired &&
            document.querySelector('[name="show_totals"]')?.checked === desired &&
            document.querySelector('[data-pdf-template-edit-layer]')?.hidden, desired);
          assert.equal(enabled, desired);
          assert.equal(await totals.count(), desired ? 1 : 0);
          assert.equal(await checkbox.isChecked(), desired);
        }
        assert.equal(submissions, 2);
        assert.deepEqual(errors, []);
        await page.close();
      }
    }
    console.log('PDF totals: all four types, desktop/mobile, checkbox defaults, POST on/off and reloaded preview OK.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
