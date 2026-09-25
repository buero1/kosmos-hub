// Real rendered drawer/editor, mocked HTTP only. Never sends customer email.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const isOrder = process.argv.includes('--orders');
const kind = isOrder ? 'orders' : 'invoices';
const noun = isOrder ? 'order' : 'invoice';
const templateName = isOrder ? 'Auftragsbestätigung' : 'Rechnungen senden';
const root = path.resolve(`tmp/${noun}-composer-browser`);
const data = JSON.parse(fs.readFileSync(path.join(root, 'data.json'), 'utf8'));
const html = fs.readFileSync(path.join(root, `${noun}.html`), 'utf8');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const width of [1263, 390]) {
      for (const scenario of ['success', 'save-error', 'send-error', 'prepare-error', ...(isOrder ? ['placeholders'] : [])]) {
        const page = await browser.newPage({viewport: {width, height: 912}});
        const errors = [], sent = [], saves = [];
        let releaseSave;
        const saveGate = new Promise(resolve => { releaseSave = resolve; });
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', async route => {
          const request = route.request(), url = new URL(request.url());
          if (url.hostname !== 'hub.test') return route.abort();
          if (url.pathname === '/emails/compose/options') return route.fulfill({json: {
            senders: [{email: data.context.sender_email, name: 'Test sender', can_send: true}],
            templates: [{id: data.context.template_id, name: templateName, module: 'Accounts'}],
            default_sender_email: data.context.sender_email,
          }});
          if (url.pathname.endsWith('/email-compose')) {
            assert.equal(request.method(), 'POST');
            return scenario === 'prepare-error'
              ? route.fulfill({status: 400, json: {detail: 'Keine fertige ZUGFeRD-PDF vorhanden.'}})
              : route.fulfill({json: scenario === 'placeholders' ? {...data.context, content: '<p>${Customer.AppointmentAt}</p>'} : data.context});
          }
          if (url.pathname === '/emails/drafts') {
            saves.push(request.postData());
            await saveGate;
            return scenario === 'save-error'
              ? route.fulfill({status: 400, json: {detail: 'Test save failed'}})
              : route.fulfill({json: {draft_id: data.context.draft_id, attachments: data.context.attachments, uploaded_attachment_ids: []}});
          }
          if (url.pathname.endsWith('/communications/emails') || url.pathname === '/emails/send') {
            sent.push(request.postData());
            return scenario === 'send-error'
              ? route.fulfill({status: 400, json: {detail: 'Versand fehlgeschlagen. Der Entwurf bleibt erhalten.'}})
              : route.fulfill({json: {redirect_url: `/finance/${kind}/${data[noun]}?email=success`}});
          }
          if (url.pathname === `/finance/${kind}/${data[noun]}`) return route.fulfill({contentType: 'text/html', body: html});
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
        await page.goto(`http://hub.test/finance/${kind}/${data[noun]}`);
        await page.locator('[data-customer-edit-open]').click();
        assert.equal(await page.locator(`[data-${noun}-email-compose]`).isVisible(), false);
        await page.locator('[data-customer-edit-cancel]').click();
        await page.locator(`[data-${noun}-email-compose]`).click();
        const drawer = page.locator('[data-global-mailbox-composer]');
        const status = drawer.locator('[data-email-compose-template-status]');
        if (scenario === 'prepare-error') {
          await status.filter({hasText: 'Keine fertige'}).waitFor();
          assert.equal(sent.length, 0);
          assert.equal(saves.length, 0);
        } else {
          await status.filter({hasText: scenario === 'placeholders' ? 'fehlende Angaben' : isOrder ? 'Auftragsvorlage und PDF' : 'Rechnungsvorlage und PDF'}).waitFor();
          assert.equal(sent.length, 0);
          assert.equal(saves.length, 0);
          assert.equal(await drawer.locator('[name="subject"]').inputValue(), data.context.subject);
          assert.equal(await drawer.locator('[name="recipient_email"]').inputValue(), data.context.recipient_email);
          assert.equal(await drawer.locator('[data-global-mailbox-template-search]').inputValue(), templateName);
          assert.equal(await drawer.locator('[name="recipient_customer_id"]').inputValue(), String(data.context.customer_id));
          assert.equal(await drawer.locator('[data-email-compose-attachment-list] a').count(), 1);
          assert.match(await drawer.locator('[data-email-compose-attachment-list]').innerText(), isOrder ? /AU-TEST.pdf/ : /RE-TEST.pdf/);
          assert.equal(await drawer.locator('[name="scheduled_at"]').isVisible(), false);
          await drawer.locator('[name="subject"]').fill('My edited invoice subject');
          const editor = drawer.frameLocator('.jodit-wysiwyg_iframe').locator('body');
          await editor.fill('Manually changed invoice email');
          await page.screenshot({path: path.join(root, `drawer-${width}-${scenario}.png`)});
          const bounds = await drawer.locator('[data-email-compose-drawer]').boundingBox();
          assert.ok(bounds.x >= -1 && bounds.x + bounds.width <= width + 1);
          await drawer.locator('[data-global-mailbox-compose-submit]').click();
          // A second submit while the first one is pending must not send again.
          await drawer.locator('form').evaluate(form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
          releaseSave();
          if (scenario === 'success' || scenario === 'placeholders') {
            await page.waitForURL(url => url.searchParams.get('email') === 'success');
            assert.equal(sent.length, 1);
            assert.match(sent[0], /My edited invoice subject/);
            assert.match(sent[0], /Manually changed invoice email/);
            assert.match(sent[0], new RegExp(data.context.attachments[0].id));
            assert.match(sent[0], /name="draft_id"/);
          } else {
            await status.filter({hasText: scenario === 'save-error' ? 'keine E-Mail versendet' : 'Versand fehlgeschlagen'}).waitFor();
            assert.equal(sent.length, scenario === 'save-error' ? 0 : 1);
            assert.equal(await drawer.locator('[name="subject"]').inputValue(), 'My edited invoice subject');
            assert.match(await editor.innerText(), /Manually changed/);
            assert.equal(await drawer.locator('[data-email-compose-attachment-list] a').count(), 1);
          }
          assert.equal(saves.length, 1);
        }
        assert.deepEqual(errors, []);
        await page.close();
      }
    }
    console.log(`${noun} composer: template/PDF, customer link, edited manual send, duplicate guard and recoverable errors passed at 1263/390px.`);
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
