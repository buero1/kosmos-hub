// Real DOM regression: failure never replaces editors, external rows or files.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

const source = fs.readFileSync('app/static/form-submit.js', 'utf8');
const html = `<!doctype html><main class="app-main-content"><h1>Edit</h1>
<button id="save" type="submit" form="edit" name="intent" value="save">Save</button>
<form id="edit" action="/save" method="post"><input name="csrf_token" value="test" type="hidden">
<input name="name" value="Original"><textarea name="text">Original</textarea>
<select name="customer"><option value="1">First</option><option value="2">Second</option></select>
<input name="checked" type="checkbox"><input name="files" type="file" multiple>
<input name="rich_text" type="hidden"><div id="rich" contenteditable="true">Original</div>
</form><section id="positions"><input name="line__0__name" value="First" form="edit">
<input name="line__0__price" value="100" form="edit"></section>
<form id="ajax" action="/custom" method="post"><button>Own AJAX</button></form>
<form id="search" action="/search" method="get"><button>Search</button></form>
<dialog open><form method="dialog"><button>Close dialog</button></form></dialog>
<script>document.querySelector('#edit').addEventListener('submit', () => {
 document.querySelector('[name="rich_text"]').value = document.querySelector('#rich').innerHTML;
}); document.querySelector('#ajax').addEventListener('submit', e => e.preventDefault());</script>
<script>${source}</script></main>`;

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    const page = await browser.newPage();
    let response = {status: 400, contentType: 'application/json', body: JSON.stringify({detail: 'Position: Einzelpreis netto ist erforderlich.'})};
    let requests = [];
    let paused;
    let pause = false;
    let networkFailure = false;
    let recordName = 'Saved record';
    let recordReads = 0;
    await page.route('http://hub.test/**', async route => {
      if (route.request().url().endsWith('/save')) {
        requests.push(route.request());
        const submittedName = route.request().postDataBuffer().toString().match(/name="record_name"\r\n\r\n([^\r]*)/);
        if (submittedName) recordName = submittedName[1];
        if (pause) await new Promise(resolve => { paused = resolve; });
        if (networkFailure) return route.abort();
        return route.fulfill(response);
      }
      if (route.request().url().includes('/record?')) {
        recordReads += 1;
        return route.fulfill({status: 200, contentType: 'text/html', body: `<!doctype html>
          <p class="notice-success">Saved</p><p id="readonly">${recordName}</p>
          <button id="edit-open">Edit</button><div id="edit-actions" hidden>
          <button type="button">Cancel</button><button id="save" type="submit" form="record">Save</button></div>
          <form id="record" action="/save" method="post" hidden><input name="record_name" value="${recordName}"></form>
          <script>document.querySelector('#edit-open').onclick = function () {
            this.hidden = true; document.querySelector('#readonly').hidden = true;
            document.querySelector('#edit-actions').hidden = false; document.querySelector('#record').hidden = false;
          };</script><script>${source}</script>`});
      }
      return route.fulfill({status: 200, contentType: 'text/html', body: route.request().url().includes('/success') ? '<h1>Saved</h1>' : html});
    });
    await page.goto('http://hub.test/edit');
    await page.locator('[name="name"]').fill('Changed name');
    await page.locator('[name="text"]').fill('Changed text');
    await page.locator('[name="customer"]').selectOption('2');
    await page.locator('[name="checked"]').check();
    await page.locator('[name="files"]').setInputFiles({name: 'offer.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-test')});
    await page.locator('#rich').fill('Changed HTML');
    await page.evaluate(() => {
      document.querySelector('[name="line__0__price"]').value = '';
      document.querySelector('#positions').insertAdjacentHTML('beforeend', '<input name="line__1__name" value="New row" form="edit">');
      window.originalForm = document.querySelector('#edit');
      window.originalFile = document.querySelector('[name="files"]').files[0];
    });
    for (const failure of ['json', 'html', '422', 'network', '401', '500']) {
      networkFailure = failure === 'network';
      if (failure === 'html') response = {status: 400, contentType: 'text/html', body: '<p class="notice-error">Required field &lt;unsafe&gt;</p><script>window.injected=true</script>'};
      else if (failure === '422') response = {status: 422, contentType: 'application/json', body: JSON.stringify({detail: [{loc: ['body', 'price'], msg: 'Field required'}]})};
      else if (failure === '401' || failure === '500') response = {status: Number(failure), contentType: 'text/plain', body: 'Internal error'};
      await page.locator('#save').click();
      await page.locator('.hub-form-error:visible').waitFor();
      await page.waitForFunction(() => !document.querySelector('#save').disabled);
      assert.equal(page.url(), 'http://hub.test/edit');
      assert.equal(await page.locator('[name="name"]').inputValue(), 'Changed name');
      assert.equal(await page.locator('[name="text"]').inputValue(), 'Changed text');
      assert.equal(await page.locator('[name="customer"]').inputValue(), '2');
      assert.equal(await page.locator('[name="checked"]').isChecked(), true);
      assert.equal(await page.locator('[name="line__1__name"]').inputValue(), 'New row');
      assert.equal(await page.locator('[name="line__0__price"]').inputValue(), '');
      assert.ok(await page.evaluate(() => window.originalForm === document.querySelector('#edit') && window.originalFile === document.querySelector('[name="files"]').files[0] && !window.injected));
      if (failure === 'json') assert.match(await page.locator('.hub-form-error').textContent(), /Einzelpreis netto/);
      if (failure === 'html') assert.equal(await page.locator('.hub-form-error').textContent(), 'Required field <unsafe>');
      const body = requests.at(-1).postDataBuffer().toString();
      assert.match(body, /Changed HTML/);
      assert.match(body, /New row/);
      assert.match(body, /%PDF-test/);
      assert.match(body, /name="intent"\r\n\r\nsave/);
      assert.equal(requests.at(-1).headers()['x-hub-form'], 'preserve');
    }
    networkFailure = false;
    pause = true;
    response = {status: 400, contentType: 'application/json', body: '{"detail":"Still missing price"}'};
    const count = requests.length;
    await page.locator('#save').click();
    await page.waitForFunction(() => document.querySelector('#save').disabled);
    await page.evaluate(() => document.querySelector('#edit').requestSubmit());
    await page.waitForTimeout(100);
    assert.equal(requests.length, count + 1, 'No duplicate saves while pending');
    paused();
    await page.waitForFunction(() => !document.querySelector('#save').disabled);
    await page.locator('#ajax button').click();
    assert.equal(requests.length, count + 1, 'Existing AJAX handlers retain ownership');
    pause = false;
    await page.locator('[name="line__0__price"]').fill('150');
    response = {status: 200, contentType: 'application/json', body: '{"redirect_url":"/success#fields"}'};
    await page.locator('#save').click();
    await page.waitForURL('http://hub.test/success#fields');
    // An existing success URL must reload too, whether its fragment is identical,
    // different or absent. A hash-only navigation cannot leave edit mode.
    const recordUrl = 'http://hub.test/record?fields=success&fields_message=Saved';
    for (const fragment of ['#fields', '#elsewhere', '']) {
      await page.goto(recordUrl + fragment);
      for (let attempt = 1; attempt <= 2; attempt += 1) {
        await page.locator('#edit-open').click();
        const editedName = 'Saved version ' + fragment + attempt;
        await page.locator('[name="record_name"]').fill(editedName);
        const beforeReads = recordReads;
        response = {status: 200, contentType: 'application/json', body: JSON.stringify({redirect_url: recordUrl + '#fields'})};
        await page.locator('#save').click();
        await page.locator('#record').waitFor({state: 'hidden', timeout: 5000});
        assert.equal(recordReads, beforeReads + 1, 'Successful same-page save must fetch fresh read-only data');
        assert.equal(await page.locator('#readonly').textContent(), editedName);
        assert.equal(await page.locator('#edit-actions').isVisible(), false);
        assert.equal(await page.locator('#edit-open').isVisible(), true);
        assert.equal(page.url(), recordUrl + '#fields');
      }
    }
    console.log('Browser OK: failure preserves all inputs; success reloads read mode, including repeated same-URL and hash-only saves; no duplicate POST.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
