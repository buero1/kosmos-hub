// Actual renderer UI with fake IPC only: never connects to a live Hub.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    const page = await browser.newPage({viewport: {width: 512, height: 445}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(/^https?:/, route => route.abort());
    await page.addInitScript(() => {
      window.testCalls = [];
      window.kosmosNotifier = {
        getState: async () => ({configured: true}),
        onUpdate(callback) { window.testUpdate = callback; }, onError() {}, onRefresh() {}, refresh() {},
        openHubPath: async path => window.testCalls.push(['open', path]),
        complete: async ids => window.testCalls.push(['complete', ids]),
        snooze: async (...args) => window.testCalls.push(['snooze', ...args]),
        snoozeBeforeStart: async (...args) => window.testCalls.push(['beforeStart', ...args]),
      };
    });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../desktop-notifier/renderer/index.html')).href);
    const item = (id, overrides = {}) => ({id, activity_name: 'Testanruf', activity_kind: 'call', activity_label: 'Anruf',
      activity_url: '/activities/call/3', starts_at: '2020-01-01T10:00:00Z', related_label: 'Kunde',
      related_url: '/customers/5', related_name: 'Beispielfirma & Partner', ...overrides});
    const update = items => page.evaluate(reminders => window.testUpdate({reminders}), items);
    await update([item(1)]);
    assert.equal(await page.locator('[data-reminder-select]').isChecked(), true);
    assert.equal(await page.locator('.reminder.is-selected').count(), 1);
    await page.getByRole('button', {name: 'Beispielfirma & Partner', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => window.testCalls), [['open', '/customers/5']]);
    assert.equal(await page.locator('[data-reminder-select]').isChecked(), true);
    await page.locator('#bulk-complete-button').click();
    assert.deepEqual(await page.evaluate(() => window.testCalls[1]), ['complete', [1]]);
    await page.locator('[data-reminder-select]').uncheck();
    await update([item(1)]);
    assert.equal(await page.locator('[data-reminder-select]').isChecked(), false);
    await update([item(1), item(2, {related_label: 'Lead', related_name: 'Erika Beispiel', related_url: '/leads/8'})]);
    assert.equal(await page.locator('[data-reminder-select]:checked').count(), 0);
    await page.locator('#select-all').check();
    assert.equal(await page.locator('[data-reminder-select]:checked').count(), 2);
    await update([item(2, {related_label: 'Lead', related_name: 'Erika Beispiel', related_url: '/leads/8'})]);
    assert.equal(await page.locator('[data-reminder-select]').isChecked(), true);
    await page.getByRole('button', {name: 'Erika Beispiel', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => window.testCalls.at(-1)), ['open', '/leads/8']);
    await page.getByRole('button', {name: 'Anruf \u00f6ffnen', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => window.testCalls.at(-1)), ['open', '/activities/call/3']);
    for (const width of [512, 440]) {
      await page.setViewportSize({width, height: 445});
      const longName = 'Beispielunternehmen-fuer-Gebaeudetechnik-und-Instandhaltung & Partner GmbH';
      await update([item(3, {related_name: longName})]);
      const link = page.getByRole('button', {name: longName, exact: true});
      const box = await link.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
    const output = path.resolve(__dirname, '../server/outputs/desktop-reminder-names.png');
    fs.mkdirSync(path.dirname(output), {recursive: true});
    await page.screenshot({path: output});
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({customer_link: true, lead_link: true, single_selected: true,
      manual_deselection_preserved: true, multi_selection: true, widths: [512, 440], live_requests: 0, screenshot: output}));
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
