const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const filename = new URL(route.request().url()).pathname.slice(1) || 'calls';
      const fixture = path.resolve('tmp/activity-party-browser', filename + '.html');
      if (fs.existsSync(fixture)) return route.fulfill({contentType:'text/html', body:fs.readFileSync(fixture)});
      return route.abort();
    });
    for (const width of [1263, 390]) {
      await page.setViewportSize({width, height:912});
      for (const [kind, plural] of [['call','calls'], ['task','tasks'], ['meeting','meetings']]) {
        await page.goto('http://hub.test/' + plural);
        const opener = page.locator('[data-customer-' + kind + '-edit]').first();
        const row = opener.locator('xpath=ancestor::tr');
        const expectedId = await row.getAttribute('data-activity-assignee-id');
        await opener.click();
        const form = page.locator('[data-customer-' + kind + '-form]');
        const owner = form.locator('[data-activity-assignee]');
        await owner.waitFor({state:'visible'});
        assert.equal(await owner.inputValue(), expectedId);
        assert.equal(await owner.isEnabled(), true);
        const bounds = await owner.boundingBox();
        assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1);
        await page.screenshot({path:'tmp/responsibility-' + kind + '-' + width + '.png'});
        await form.locator('[data-customer-activity-compose-close]').click();
        await row.evaluate(element => Object.assign(element.dataset, {
          activityAssigneeId:'999', activityAssigneeName:'Other Employee', activityCanEdit:'false', activityCanAssign:'false'
        }));
        await opener.click();
        assert.equal(await owner.inputValue(), '999');
        assert.equal(await form.locator('[name="name"]').isDisabled(), true);
        assert.equal(await form.locator('[type="submit"]').isVisible(), false);
        await page.evaluate(kind => document.dispatchEvent(new CustomEvent('calendar:activity-open', {detail:{kind}})), kind);
        assert.equal(await owner.inputValue(), expectedId, 'New form must restore the default user');
        assert.equal(await owner.locator('option[value="999"]').count(), 0);
        assert.equal(await form.locator('[name="name"]').isEnabled(), true);
        assert.equal(await form.locator('[type="submit"]').isVisible(), true);
      }
    }
    await page.goto('http://hub.test/calendar');
    const event = page.locator('[data-calendar-activity-edit]').first();
    const ownerId = await event.getAttribute('data-activity-assignee-id');
    await event.click();
    const activeForm = page.locator('.activity-compose-form:visible');
    assert.equal(await activeForm.locator('[data-activity-assignee]').inputValue(), ownerId);
    assert.equal(errors.length, 0, errors.join('\n'));
    console.log('Activity responsibility: desktop/mobile, all 3 drawers, readonly permissions, reset, calendar passed.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
