const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
  try {
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const fixtures = Array.from({length: 110}, (_, index) => ({record_id: String(index + 1), name: `Kunde ${String(index + 1).padStart(3, '0')}`,
        detail: index === 0 ? '<img src=x onerror=alert(1)>' : 'beispiel.de', status: 'Aktiv', owner: index % 2 ? 'Anna' : '', team: index % 3 ? 'Service' : ''}));
      const posts = [];
      const savedTeams = {customers: {'1': ['1', '2', '105'], '2': ['3']}, leads: {'1': ['1'], '2': []}};
      let rejectSave = true, rejectLoad = false;
      await page.route('http://hub.test/**', async route => {
        const url = new URL(route.request().url());
        if (url.pathname.startsWith('/static/')) {
          const filename = path.join('app', url.pathname);
          return route.fulfill({body: fs.readFileSync(filename), contentType: filename.endsWith('.css') ? 'text/css' : 'text/javascript'});
        }
        if (url.pathname === '/account/access/records/options') {
          const q = url.searchParams.get('query').toLowerCase();
          const module = url.searchParams.get('module_key');
          const records = module === 'leads' ? fixtures.slice(0, 2).map(row => ({...row, name: 'Lead ' + row.record_id})) : fixtures;
          const items = records.filter(row => row.name.toLowerCase().includes(q));
          const team = url.searchParams.get('team_id');
          const saved = team ? savedTeams[module][team] : null;
          if (rejectLoad) return route.fulfill({status: 400, json: {detail: 'Team konnte nicht geladen werden.'}});
          if (q === 'slow') await new Promise(resolve => setTimeout(resolve, 700));
          return route.fulfill({json: {items, total: items.length, next_offset: '',
            ...(saved ? {previous_record_ids: saved, selected_items: records.filter(row => saved.includes(row.record_id))} : {})}});
        }
        if (url.pathname === '/account/access/records/batch') {
          posts.push(route.request().postData());
          if (rejectSave) return route.fulfill({status: 400, json: {detail: 'Testfehler: Team nicht mehr aktiv.'}});
          const fields = await new Response(route.request().postDataBuffer(), {
            headers: {'content-type': route.request().headers()['content-type']}
          }).formData();
          const module = fields.get('module_key'), team = fields.get('team_id');
          assert.deepEqual(JSON.parse(fields.get('previous_record_ids')), savedTeams[module][team]);
          savedTeams[module][team] = JSON.parse(fields.get('record_ids'));
          return route.fulfill({json: {message: 'Gespeichert', redirect_url: '/settings?access=assignment-saved#account-access'}});
        }
        return route.fulfill({contentType: 'text/html', body: fs.readFileSync('tmp/access-picker-preview.html')});
      });
      await page.goto('http://hub.test/');
      const forms = page.locator('[data-access-record-form]');
      const open = forms.nth(0).locator('[data-access-record-open]');
      const layer = page.locator('[data-access-record-layer]');
      const rows = page.locator('[data-access-record-rows] tr');
      const count = page.locator('[data-access-record-count]');
      const selection = async () => JSON.parse(await forms.nth(0).locator('[name=record_ids]').inputValue());
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent.startsWith('110 Treffer'));
      assert.equal(await rows.count(), 100);
      assert.equal(await page.locator('[data-access-record-page]').textContent(), 'Seite 1 von 2');
      assert.equal(await layer.locator('img').count(), 0);
      await rows.nth(0).locator('input').click();
      await rows.nth(2).locator('input').click({modifiers: ['Shift']});
      assert.equal(await count.textContent(), '3 ausgewählt');
      await rows.nth(4).locator('td').nth(1).click({modifiers: ['Control']});
      assert.equal(await count.textContent(), '4 ausgewählt');
      await rows.nth(4).locator('td').nth(1).click({modifiers: ['Control']});
      assert.equal(await count.textContent(), '3 ausgewählt');
      await page.locator('[data-access-record-all]').check();
      assert.equal(await count.textContent(), '110 ausgewählt');
      await rows.nth(0).locator('input').uncheck();
      assert.equal(await count.textContent(), '109 ausgewählt');
      // A click in the checkbox cell margin must not replace the selection.
      await rows.nth(1).locator('td').first().click({position: {x: 3, y: 3}});
      assert.equal(await count.textContent(), '108 ausgewählt', 'Cell-margin click must retain all other selected records');
      await rows.nth(2).locator('td').nth(1).click();
      assert.equal(await count.textContent(), '107 ausgewählt', 'Plain row click must toggle only that row');
      await rows.nth(2).locator('td').nth(1).click();
      assert.equal(await count.textContent(), '108 ausgewählt');
      await rows.nth(5).locator('td').nth(1).click({modifiers: ['Shift']});
      assert.equal(await count.textContent(), '104 ausgewählt', 'Shift deselection must retain selections outside the range');
      await rows.nth(5).locator('td').nth(1).click({modifiers: ['Shift']});
      assert.equal(await count.textContent(), '108 ausgewählt', 'Shift selection must retain selections outside the range');
      await page.locator('[data-access-record-all]').check();
      assert.equal(await count.textContent(), '110 ausgewählt');
      await page.locator('[data-access-record-next]').click();
      assert.equal(await rows.count(), 10);
      assert.equal(await rows.locator('input:checked').count(), 10);
      await rows.nth(0).locator('td').nth(1).click();
      assert.equal(await count.textContent(), '109 ausgewählt');
      await page.locator('[data-access-record-prev]').click();
      assert.equal(await rows.locator('input:checked').count(), 100, 'Deselection on page 2 must preserve all of page 1');
      await page.locator('[data-access-record-all]').check();
      await page.locator('[data-access-record-search]').fill('Kunde 110');
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent.startsWith('1 Treffer'));
      await page.locator('[data-access-record-all]').uncheck();
      assert.equal(await count.textContent(), '109 ausgewählt');
      await page.locator('[data-access-record-apply]').click();
      assert.equal((await selection()).length, 109);
      assert.equal(await open.evaluate(el => el === document.activeElement), true);
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent.startsWith('110 Treffer'));
      await page.locator('[data-access-record-clear]').click();
      await page.keyboard.press('Escape');
      assert.equal((await selection()).length, 109, 'Cancel must not change the applied selection');
      await forms.nth(0).locator('[name=owner_user_id]').selectOption('1');
      await forms.nth(0).locator('[type=submit]').click();
      await forms.nth(0).locator('.hub-form-error').waitFor();
      assert.equal(posts.length, 1);
      assert.equal((await selection()).length, 109);
      assert.equal(await forms.nth(0).locator('[name=owner_user_id]').inputValue(), '1');
      assert.ok(posts[0].includes('109') && posts[0].includes('__keep__'));
      await forms.nth(0).locator('[name=module_key]').selectOption('leads');
      assert.equal((await selection()).length, 0);
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent.startsWith('2 Treffer'));
      await rows.nth(0).locator('input').focus();
      await page.keyboard.press('Space');
      assert.equal(await count.textContent(), '1 ausgewählt');
      await page.locator('[data-access-record-apply]').focus();
      await page.keyboard.press('Tab');
      assert.equal(await drawerFirstFocused(page), true, 'Tab must stay inside the drawer');
      await page.keyboard.press('Escape');
      // Saved assignments are loaded after a fresh page visit, not just cached in this drawer.
      await page.reload();
      await forms.nth(0).locator('[name=team_id]').selectOption('1');
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-count]').textContent === '3 ausgewählt');
      assert.equal(await rows.locator('input:checked').count(), 2);
      await rows.nth(0).locator('input').uncheck();
      await rows.nth(3).locator('input').check();
      await page.locator('[data-access-record-next]').click();
      assert.equal(await rows.locator('input:checked').count(), 1);
      await page.locator('[data-access-record-search]').fill('Kunde 004');
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent.startsWith('1 Treffer'));
      assert.equal(await count.textContent(), '3 ausgewählt', 'Search must not reapply saved data over pending edits');
      await page.locator('[data-access-record-apply]').click();
      assert.deepEqual((await selection()).sort(), ['105', '2', '4']);
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent.startsWith('110 Treffer'));
      assert.equal(await rows.nth(0).locator('input').isChecked(), false, 'Reopening must keep unapplied server changes local');
      await page.keyboard.press('Escape');
      rejectSave = false;
      await forms.nth(0).locator('[type=submit]').click();
      await page.waitForURL('**/settings?access=assignment-saved#account-access');
      await forms.nth(0).locator('[name=team_id]').selectOption('1');
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-count]').textContent === '3 ausgewählt');
      assert.equal(await rows.nth(0).locator('input').isChecked(), false);
      assert.equal(await rows.nth(1).locator('input').isChecked(), true);
      assert.equal(await rows.nth(3).locator('input').isChecked(), true);
      await page.locator('[data-access-record-apply]').click();
      await forms.nth(0).locator('[name=team_id]').selectOption('2');
      assert.deepEqual(await selection(), []);
      await forms.nth(0).locator('[type=submit]').click();
      assert.equal(posts.length, 2, 'Must load team snapshot before saving a different team');
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-count]').textContent === '1 ausgewählt');
      assert.equal(await rows.nth(2).locator('input').isChecked(), true);
      await page.keyboard.press('Escape');
      // Failed loading must not allow an empty selection to clear a team.
      rejectLoad = true;
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent === 'Team konnte nicht geladen werden.');
      assert.equal(await page.locator('[data-access-record-apply]').isDisabled(), true);
      await page.keyboard.press('Escape');
      rejectLoad = false;
      await forms.nth(0).locator('[name=team_id]').selectOption('1');
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-count]').textContent === '3 ausgewählt');
      await page.locator('[data-access-record-clear]').click();
      await page.locator('[data-access-record-apply]').click();
      await forms.nth(0).locator('[type=submit]').click();
      await page.waitForFunction(() => document.querySelector('[name=team_id]').value === '__keep__');
      assert.deepEqual(savedTeams.customers['1'], [], 'An explicitly empty selection must be persisted');
      assert.deepEqual(savedTeams.customers['2'], ['3'], 'Other teams must remain unchanged');
      await forms.nth(0).locator('[name=module_key]').selectOption('leads');
      await forms.nth(0).locator('[name=team_id]').selectOption('1');
      await open.click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent.startsWith('2 Treffer'));
      assert.equal(await rows.nth(0).locator('input').isChecked(), true, 'Lead assignments are also prechecked');
      await page.keyboard.press('Escape');
      await forms.nth(1).locator('[data-access-record-open]').click();
      await page.waitForFunction(() => document.querySelector('[data-access-record-status]').textContent.startsWith('110 Treffer'));
      assert.equal(await count.textContent(), '0 ausgewählt');
      await rows.nth(0).locator('input').check();
      await rows.nth(1).locator('input').check();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
      assert.equal(overflow, false, 'No viewport overflow');
      await page.screenshot({path: `tmp/access-picker-${width}.png`});
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px: selection/ranges/pages, saved team roundtrip/reload, add/remove/empty, team/module switch, load errors, keyboard`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});

async function drawerFirstFocused(page) {
  return page.locator('.access-record-drawer [data-access-record-close]').first().evaluate(el => el === document.activeElement);
}
