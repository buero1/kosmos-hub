const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, channel:'chrome'});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let outcome = 'failure';
    let posts = [];
    let finishPending;
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === 'POST') {
        posts.push({url:url.pathname, data:request.postData()});
        if (outcome === 'network') return route.abort();
        if (outcome === 'pending') await new Promise(resolve => {finishPending = resolve;});
        return route.fulfill({status:outcome === 'failure' ? 400 : 200, contentType:'application/json',
          body:JSON.stringify(outcome === 'failure' ? {detail:'Keine Berechtigung zum Loeschen.'} : {ok:true, message:'Deleted'})});
      }
      if (url.pathname === '/static/activity-panel-delete.js') return route.fulfill({contentType:'text/javascript', body:fs.readFileSync('app/static/activity-panel-delete.js')});
      const fixture = path.resolve('tmp/activity-party-browser', url.pathname.slice(1) + '.html');
      if (fs.existsSync(fixture)) return route.fulfill({contentType:'text/html', body:fs.readFileSync(fixture)});
      return route.abort();
    });
    for (const width of [1263, 390, 320]) {
      await page.setViewportSize({width, height:912});
      for (const [kind, plural, label] of [['call','calls','Anruf'], ['task','tasks','Aufgabe'], ['meeting','meetings','Meeting']]) {
        await page.goto('http://hub.test/' + plural + '?view=created&marker=keep');
        posts = [];
        outcome = 'failure';
        assert.equal(await page.locator('[data-activity-delete-open]:visible').count(), 0);
        assert.equal(await page.locator('tbody [data-activity-delete-open]').count(), 0, 'No list actions added');
        const opener = page.locator('[data-customer-' + kind + '-edit]').first();
        const row = opener.locator('xpath=ancestor::tr');
        const identity = await row.getAttribute('data-activity-id');
        await opener.click();
        const form = page.locator('[data-customer-' + kind + '-form]');
        const button = form.locator('[data-activity-delete-open]');
        await button.waitFor({state:'visible'});
        assert.equal((await button.textContent()).trim(), label + ' l\u00f6schen');
        const statusBounds = await form.locator('[name="status"]').boundingBox();
        const buttonBounds = await button.boundingBox();
        assert.ok(buttonBounds.x >= statusBounds.x + statusBounds.width, 'Delete must be right of Status');
        assert.ok(Math.abs(buttonBounds.y + buttonBounds.height - statusBounds.y - statusBounds.height) <= 2);
        assert.ok(buttonBounds.x + buttonBounds.width <= width + 1);
        await form.locator('[name="name"]').fill('Unsaved edits must remain');
        await button.click();
        const dialog = page.locator('[data-activity-panel-delete-dialog]');
        const confirm = dialog.locator('[data-activity-panel-delete-confirm]');
        const cancel = dialog.locator('[data-activity-panel-delete-cancel]');
        await dialog.waitFor({state:'visible'});
        assert.equal(await dialog.locator('p').nth(1).textContent(), 'Zugeh\u00f6rige Erinnerungen werden entfernt und ausstehende Erinnerungs-E-Mails storniert.');
        await page.keyboard.press('Escape');
        await dialog.waitFor({state:'hidden'});
        assert.equal(await form.isVisible(), true, 'Escape must not close the editing panel');
        assert.equal(posts.length, 0);
        await button.click();
        await cancel.click();
        assert.equal(posts.length, 0);
        assert.equal(await form.locator('[name="name"]').inputValue(), 'Unsaved edits must remain');
        await button.click();
        await confirm.click();
        await dialog.locator('[data-activity-panel-delete-error]').waitFor({state:'visible'});
        assert.equal(posts.length, 1);
        assert.equal(posts[0].url, '/activities/' + kind + '/' + identity + '/delete');
        assert.ok(posts[0].data.includes('csrf_token'));
        assert.ok(!posts[0].data.includes('Unsaved edits'));
        assert.equal(await form.locator('[name="name"]').inputValue(), 'Unsaved edits must remain');
        assert.equal(await form.isVisible(), true);
        await cancel.click();
        await page.screenshot({path:'tmp/activity-delete-' + kind + '-' + width + '.png'});
        outcome = 'network';
        await button.click();
        await confirm.click();
        await dialog.locator('[data-activity-panel-delete-error]').waitFor({state:'visible'});
        assert.ok((await dialog.locator('[data-activity-panel-delete-error]').textContent()).includes('nicht bestaetigt'));
        await cancel.click();
        outcome = 'pending';
        await button.click();
        const response = page.waitForResponse(response => response.request().method() === 'POST');
        await confirm.click();
        await page.waitForFunction(() => document.querySelector('[data-activity-panel-delete-dialog]').getAttribute('aria-busy') === 'true');
        await page.keyboard.press('Escape');
        assert.equal(await dialog.isVisible(), true);
        assert.equal(await cancel.isDisabled(), true);
        await confirm.evaluate(element => element.click());
        const navigation = page.waitForEvent('load');
        while (!finishPending) await page.waitForTimeout(10);
        finishPending();
        finishPending = null;
        await response;
        await navigation;
        assert.equal(posts.length, 3, 'Double submission must not send a second request');
        assert.equal(new URL(page.url()).searchParams.get('view'), 'created');
        assert.equal(new URL(page.url()).searchParams.get('marker'), 'keep');
        assert.equal(await page.locator('[data-customer-activity-compose]').isVisible(), false);
        await page.locator('[data-activity-id]').first().evaluate(element => Object.assign(element.dataset, {activityCanDelete:'false'}));
        await page.locator('[data-customer-' + kind + '-edit]').first().click();
        assert.equal(await button.isVisible(), false);
        await page.evaluate(kind => document.dispatchEvent(new CustomEvent('calendar:activity-open', {detail:{kind}})), kind);
        assert.equal(await button.isVisible(), false, 'New activities must not expose delete');
      }
    }
    await page.goto('http://hub.test/calendar?week=2030-10-14');
    await page.locator('[data-calendar-activity-edit]').first().click();
    assert.equal(await page.locator('.activity-compose-form:visible [data-activity-delete-open]').isVisible(), true);
    assert.deepEqual(errors, []);
    console.log('Panel deletion passed: call/task/meeting, 1263/390/320px, permissions, calendar, cancel/Escape, errors keep edits, CSRF, single POST, filter-preserving success.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
