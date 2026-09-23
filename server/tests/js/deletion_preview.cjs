const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const script = fs.readFileSync('app/static/deletion-preview.js', 'utf8');
const base = fs.readFileSync('app/templates/base.html', 'utf8');
const styles = base.match(/<style[^>]*>([\s\S]*?)<\/style>/)[1];

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      let blocked = true, failed = false, previews = 0;
      const html = `<style>${styles}</style><button id="open" onclick="document.querySelector('dialog').showModal()">Open</button>
        <dialog class="update-confirmation-dialog"><form method="post" action="/leads/5/delete">
        <input type="hidden" name="csrf_token" value="unchanged"><h3>Lead loeschen?</h3>
        <div class="dialog-actions"><button type="button" onclick="this.closest('dialog').close()">Cancel</button><button type="submit">Delete</button></div></form></dialog>
        <form id="standalone" method="post" action="/finance/offers/8/delete"><button type="submit">Standalone</button></form>
        <script>${script}</script><script>window.submitted=0;document.addEventListener('submit',e=>{e.preventDefault();window.submitted++;});</script>`;
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        assert.equal(url.host, 'hub.test');
        if (url.pathname === '/deletion-preview') {
          previews++;
          if (failed) return route.fulfill({status: 500, json: {detail: 'Testfehler'}});
          return route.fulfill({json: {deleted: ['Lead mit allen Feldern', '2 Notizen', '3 Aufgaben', '1 Anruf', 'Erinnerungen'],
            retained: ['Postfach-E-Mails samt Anhaengen', 'Angebote und PDFs', 'Protokolle'], blockers: blocked ? ['Noch 1 geplante E-Mail. Bitte zuerst entfernen.'] : []}});
        }
        return route.fulfill({contentType: 'text/html', body: html});
      });
      await page.goto('http://hub.test/leads/5');
      await page.locator('#open').click();
      await page.getByText('Noch 1 geplante E-Mail.', {exact: false}).waitFor();
      const submit = page.locator('dialog form button[type=submit]');
      assert.equal(await submit.isDisabled(), true);
      await page.locator('dialog form').evaluate(form => form.requestSubmit());
      assert.equal(await page.evaluate(() => window.submitted), 0, 'Programmatic submit cannot bypass blocker');
      await page.getByText('Noch 1 geplante E-Mail.', {exact: false}).waitFor();
      await page.locator('dialog').screenshot({path: `tmp/deletion-dialog-${width}.png`});
      const bounds = await page.locator('dialog').boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1 && bounds.height <= 912);
      assert.equal(await page.locator('dialog a').getAttribute('href'), '/emails?folder=planned');
      await page.getByText('Cancel', {exact: true}).click();
      blocked = false;
      await page.locator('#open').click();
      await page.getByText('Endg\u00fcltiges L\u00f6schen:', {exact: false}).waitFor();
      assert.equal(await submit.isEnabled(), true);
      assert.equal(await page.locator('[name=csrf_token]').inputValue(), 'unchanged');
      await submit.click();
      assert.equal(await page.evaluate(() => window.submitted), 1);
      await page.getByText('Cancel', {exact: true}).click();
      failed = true;
      await page.locator('#open').click();
      await page.getByText('Testfehler', {exact: true}).waitFor();
      assert.equal(await submit.isDisabled(), true, 'Unavailable preview fails closed');
      failed = false;
      await page.getByRole('button', {name: 'Erneut pr\u00fcfen'}).click();
      await page.getByText('Endg\u00fcltiges L\u00f6schen:', {exact: false}).waitFor();
      assert.equal(await submit.isEnabled(), true);
      await page.getByText('Cancel', {exact: true}).click();
      await page.getByText('Standalone', {exact: true}).click();
      await page.locator('dialog[open] [data-deletion-confirm]').waitFor();
      await page.waitForFunction(() => !document.querySelector('[data-deletion-confirm]').disabled);
      assert.equal(await page.evaluate(() => window.submitted), 1, 'First submit only opens confirmation');
      await page.locator('[data-deletion-confirm]').click();
      assert.equal(await page.evaluate(() => window.submitted), 2);
      assert.ok(previews >= 5);
      await page.close();
    }
    console.log('Deletion dialogs OK: desktop/mobile, blockers, cancel/reopen, failure/retry, native standalone confirmation, CSRF preserved.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
