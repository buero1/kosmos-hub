const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, ...(process.platform === 'win32' ? {channel:'chrome'} : {})});
  try {
    for (const width of [1263, 390]) {
      const page = await browser.newPage({viewport:{width,height:912}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => {
        const req = route.request();
        const url = new URL(req.url());
        if (url.hostname !== 'hub.test' || req.method() !== 'GET') return route.abort();
        if (url.pathname === '/calendar') return route.fulfill({contentType:'text/html', body:fs.readFileSync('tmp/activity-party-browser/calendar.html','utf8')});
        if (['/calls','/tasks','/meetings'].includes(url.pathname)) return route.fulfill({contentType:'text/html', body:fs.readFileSync('tmp/activity-party-browser' + url.pathname + '.html','utf8')});
        if (/^\/(customers|leads)\/\d+$/.test(url.pathname)) return route.fulfill({contentType:'text/html',body:'<h1>Linked record</h1>'});
        const file = path.resolve('app', '.' + url.pathname);
        if (url.pathname.startsWith('/static/') && file.startsWith(path.resolve('app/static') + path.sep) && fs.existsSync(file)) {
          return route.fulfill({path:file});
        }
        return route.abort();
      });
      await page.goto('http://hub.test/calendar?week=2030-10-14', {waitUntil:'domcontentloaded'});
      for (const kind of ['call','task','meeting']) {
        await page.evaluate(kind => document.dispatchEvent(new CustomEvent('calendar:activity-open',{detail:{kind}})), kind);
        const form = page.locator('[data-customer-' + kind + '-form]');
        const input = form.locator('[data-finance-party-input]');
        assert.equal(await input.isVisible(),true);
        assert.equal(await form.locator('select[name="customer_id"]').count(),0);
        await input.fill('muller');
        const customer = form.locator('[data-finance-party-option][data-party-type="customer"]:visible');
        assert.equal(await customer.count(),1);
        await customer.click();
        assert.equal(await form.locator('[name="customer_id"]').inputValue(),'1');
        assert.equal(await form.locator('[name="lead_id"]').inputValue(),'');
        if (kind !== 'task') assert.ok((await form.locator('[name="name"]').inputValue()).includes('M\u00fcller Garten'));
        await form.locator('[name="name"]').fill('Individueller Name');
        await input.fill('muster');
        assert.equal(await form.locator('[name="customer_id"]').inputValue(),'');
        assert.equal(await input.evaluate(node => node.validity.valid),false);
        await input.press('ArrowDown');
        await input.press('Enter');
        assert.equal(await form.locator('[name="lead_id"]').inputValue(),'1');
        assert.equal(await form.locator('[name="customer_id"]').inputValue(),'');
        assert.equal(await input.evaluate(node => node.validity.valid),true);
        assert.equal(await form.locator('[name="name"]').inputValue(),'Individueller Name');
        await input.fill('muster');
        await page.locator('[data-customer-activity-compose-drawer]').screenshot({path:path.resolve('tmp/activity-party-browser/' + kind + '-' + width + '.png')});
        const bounds = await form.locator('.finance-customer-options').boundingBox();
        assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1);
        await input.fill('');
        assert.equal(await form.locator('[name="lead_id"]').inputValue(),'');
        assert.equal(await input.evaluate(node => node.validity.valid),true);
        await input.fill('DoesNotExist');
        assert.equal(await input.evaluate(node => node.validity.valid),false);
        await page.evaluate(kind => document.dispatchEvent(new CustomEvent('calendar:activity-open',{detail:{kind}})),kind);
        assert.equal(await input.inputValue(),'');
        assert.equal(await input.evaluate(node => node.validity.valid),true);
        assert.equal(await form.locator('[name="lead_id"]').inputValue(),'');
      }
      await page.locator('[data-customer-activity-compose-drawer] .email-compose-close').click();
      for (const kind of ['call','meeting']) {
        const event = page.locator('[data-calendar-activity-kind="' + kind + '"]').first();
        const expectedName = await event.getAttribute('data-calendar-activity-name');
        await event.click();
        const form = page.locator('[data-customer-' + kind + '-form]');
        assert.equal(await form.locator('[name="lead_id"]').inputValue(),'1');
        assert.equal(await form.locator('[name="customer_id"]').inputValue(),'');
        assert.ok((await form.locator('[data-finance-party-input]').inputValue()).includes('Lead'));
        assert.equal(await form.locator('[name="name"]').inputValue(),expectedName);
        await page.locator('[data-customer-activity-compose-drawer] .email-compose-close').click();
      }
      assert.deepEqual(errors,[]);
      for (const [kind, plural] of [['call','calls'],['task','tasks'],['meeting','meetings']]) {
        await page.goto('http://hub.test/' + plural, {waitUntil:'domcontentloaded'});
        const form = page.locator('[data-customer-' + kind + '-form]');
        const link = form.locator('[data-customer-activity-directory-relation]');
        const empty = form.locator('[data-customer-activity-directory-relation-empty]');
        for (const owner of ['customer','none','lead','none']) {
          const opener = page.locator('[data-customer-' + kind + '-edit][data-customer-' + kind + '-name="' + owner + '"]');
          await opener.click();
          const expected = await opener.getAttribute('data-customer-activity-relation-href');
          assert.equal(await link.isVisible(),owner !== 'none');
          assert.equal(await empty.isVisible(),owner === 'none');
          assert.equal(await link.getAttribute('href'),expected || null);
          if (owner !== 'none') {
            assert.equal(await link.textContent(),await opener.getAttribute('data-customer-activity-relation'));
            const bounds = await link.boundingBox();
            assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1);
            await page.locator('[data-customer-activity-compose-drawer]').screenshot({path:path.resolve('tmp/activity-party-browser/link-' + kind + '-' + width + '.png')});
          }
          await page.locator('[data-customer-activity-compose-drawer] .email-compose-close').click();
        }
        const leadOpener = page.locator('[data-customer-' + kind + '-edit][data-customer-' + kind + '-name="lead"]');
        await leadOpener.click();
        const target = await link.getAttribute('href');
        await link.click();
        await page.waitForURL('http://hub.test' + target);
      }
      assert.deepEqual(errors,[]);
      console.log(JSON.stringify({viewport:width,threeForms:true,search:true,keyboard:true,clear:true,reset:true,existingLead:true,noScriptErrors:true}));
      console.log(JSON.stringify({viewport:width,relatedLinks:true,unlinkedFallback:true,noStaleLinks:true,navigation:true}));
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error.stack); process.exitCode=1;});
