const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const partial = fs.readFileSync(path.join(root, 'app/templates/partials/customer_website_profile.html'), 'utf8')
  .replaceAll('{{ detail.entry.customer.id }}', '1').replaceAll('{{ detail.entry.customer.name }}', 'Test')
  .replaceAll('{{ csrf_token }}', 'test-token').replace(/<link[^>]+>/g, '').replace(/<script[^>]+><\/script>/g, '');
const script = fs.readFileSync(path.join(root, 'app/static/customer-website-profile.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'app/static/customer-website-profile.css'), 'utf8');
const sharedCss = fs.readFileSync(path.join(root, 'app/templates/base.html'), 'utf8')
  .match(/\.pdf-template-edit-layer \{[\s\S]*?(?=body\.finance-position-preset-open)/)[0]
  + fs.readFileSync(path.join(root, 'app/templates/base.html'), 'utf8')
    .match(/\.finance-customer-options \{[\s\S]*?\.finance-customer-empty \{[^}]+\}/)[0];
const contacts = [
  {id: '10', name: 'Anna Example', email: 'anna@example.test'},
  {id: '20', name: 'Zoe Example', email: 'zoe@example.test'},
  {id: '40', name: 'Zoe Example', email: 'other-zoe@example.test'},
  {id: '50', name: 'Without Email', email: ''},
].map(contact => ({...contact, values: {contact_person: contact.name, email: contact.email,
  email_link: contact.email ? 'mailto:' + contact.email : '', email_break: contact.email}}));
const preview = {site_id: '1', domain: 'main.example', target_source: 'Website URL', preview_token: 'proof',
  contacts, contact_id: '10',
  options: [{site_id: '1', domain: 'main.example', source: 'Website URL'}, {site_id: '2', domain: 'work.example', source: 'Arbeitsdomain URL'}],
  rows: [
    {id: 'company_name', label: 'Firma', type: 'text', editable: true, max_length: 500, current: 'Alt', proposed: '<img src=x onerror=alert(1)>', selectable: true, source: 'Kunde-Name'},
    {id: 'phone', label: 'Telefon', type: 'phone', editable: true, max_length: 500, current: '', proposed: '+49 123', selectable: true, source: 'Tel.'},
    {id: 'contact_person', label: 'Ansprechpartner', type: 'text', editable: true, max_length: 500, current: '', proposed: 'Anna Example', selectable: true, contact_field: 'contact_person'},
    {id: 'email', label: 'Email', type: 'email', editable: true, max_length: 254, current: 'keep@example.test', proposed: 'anna@example.test', selectable: true, contact_field: 'email'},
    {id: 'email_link', label: 'Email-Link', type: 'text', editable: true, max_length: 500, current: '', proposed: 'mailto:anna@example.test', selectable: true, contact_field: 'email_link'},
    {id: 'email_break', label: 'Email_break', type: 'textarea', editable: true, max_length: 500, current: '', proposed: 'anna@example.test', selectable: true, contact_field: 'email_break'},
    {id: 'notes', label: 'Zeiten', type: 'textarea', editable: true, max_length: 10000, current: '', proposed: '', selectable: false},
    {id: 'date', label: 'Datum', type: 'date', editable: true, max_length: 500, current: '', proposed: '', selectable: false},
    {id: 'logo', label: 'Logo', type: 'image', editable: false, current: 2, proposed: '', selectable: false},
  ]};
(async () => {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    for (const width of [1300, 390]) {
      const page = await browser.newPage({viewport: {width, height: 912}});
      let sends = [], fail = 0, reads = [], rejectPreview = false;
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      await page.route('https://hub.test/**', async route => {
        const url = new URL(route.request().url());
        if (url.pathname.endsWith('/preview')) {
          reads.push(url.searchParams.get('site_id'));
          return route.fulfill({status: rejectPreview ? 409 : 200, json: rejectPreview ? {detail: 'Keine Bridge-Verbindung.'} :
            {...preview, site_id: url.searchParams.get('site_id') || '1'}});
        }
        if (url.pathname.endsWith('/send')) {
          sends.push(new URLSearchParams(route.request().postData()));
          if (fail === -1) return route.abort();
          return route.fulfill({status: fail || 200, json: fail ? {detail: fail === 422 ? 'Bitte den Wert korrigieren.' : 'Vorschau veraltet.'} : {job_id: 7}});
        }
        return route.fulfill({contentType: 'text/html', body: '<style>*{box-sizing:border-box}[hidden]{display:none!important}.table-scroll{overflow:auto}small{display:block}' + sharedCss + css + '</style><button data-website-profile-open>Open</button>' + partial});
      });
      await page.goto('https://hub.test/');
      await page.addScriptTag({content: script});
      const open = () => page.locator('[data-website-profile-open]').click();
      const send = page.locator('[data-website-profile-send]');
      const all = page.locator('[data-website-profile-all]');
      const rows = page.locator('[data-website-profile-rows]');
      const value = id => rows.locator('[data-field-id="' + id + '"]');
      await open();
      await rows.locator('tr').last().waitFor();
      assert.equal(await send.isEnabled(), true);
      assert.equal(await rows.locator('img').count(), 0, 'Remote strings never become markup');
      await all.uncheck(); assert.equal(await send.isEnabled(), false);
      await value('company_name').fill('Edited Company');
      assert.equal(await rows.locator('input[value=company_name]').isChecked(), true, 'Editing selects the field');
      await value('email').fill('not-an-email');
      await rows.locator('input[value=email]').uncheck();
      assert.equal(await value('notes').getAttribute('maxlength'), '10000');
      assert.equal(await rows.locator('[data-field-id=logo]').count(), 0);
      await send.click();
      await page.locator('a[href="/wordpress/jobs/7"]').waitFor();
      assert.deepEqual(sends[0].getAll('field_ids'), ['company_name']);
      assert.equal(sends[0].get('csrf_token'), 'test-token');
      assert.equal(sends[0].get('confirmed'), 'yes');
      assert.deepEqual(JSON.parse(sends[0].get('edited_values_json')), {company_name: 'Edited Company'}, 'Only selected values are sent, not an unselected invalid email');
      assert.equal(await send.isEnabled(), false);
      await page.locator('[data-website-profile-reload]').click();
      await rows.locator('tr').last().waitFor();
      await page.locator('[data-website-profile-target]').selectOption('2');
      await page.waitForFunction(() => document.querySelector('[data-website-profile-target]').value === '2' && !document.querySelector('[data-website-profile-send]').disabled);
      assert.equal(reads.at(-1), '2');
      await all.uncheck();
      await value('email').fill('valid@example.test');
      await value('notes').fill('Monday\nTuesday');
      await value('date').fill('2026-09-24');
      await value('phone').fill('');
      assert.equal(await rows.locator('input[value=phone]').isChecked(), false, 'Empty values never clear remote fields');
      assert.equal(await rows.locator('input[value=phone]').isDisabled(), true);
      fail = 422;
      await send.click(); await page.getByText('Bitte den Wert korrigieren.', {exact: true}).waitFor();
      assert.equal(await send.isEnabled(), true, 'Validation failure remains correctable');
      assert.equal(await value('notes').inputValue(), 'Monday\nTuesday');
      assert.equal(await value('email').inputValue(), 'valid@example.test');
      await value('notes').fill('Monday\nWednesday');
      fail = 0; await send.click();
      await page.locator('a[href="/wordpress/jobs/7"]').waitFor();
      assert.deepEqual(JSON.parse(sends.at(-1).get('edited_values_json')), {email: 'valid@example.test', notes: 'Monday\nWednesday', date: '2026-09-24'});
      await page.locator('[data-website-profile-reload]').click();
      await rows.locator('tr').last().waitFor();
      await value('company_name').fill('Keep this draft');
      fail = 409;
      await send.click(); await page.getByText('Vorschau veraltet.', {exact: true}).waitFor();
      assert.equal(await send.isEnabled(), false, 'Failure must invalidate confirmation, not auto retry');
      assert.equal(await value('company_name').inputValue(), 'Keep this draft');
      await page.locator('[data-website-profile-reload]').click();
      await rows.locator('tr').last().waitFor();
      fail = -1;
      await send.click(); await page.locator('[data-website-profile-error]:not([hidden])').waitFor();
      assert.equal(await send.isEnabled(), false, 'Transport failure never offers a blind retry');
      const before = sends.length;
      await page.getByRole('button', {name: 'Abbrechen', exact: true}).click();
      await open(); await rows.locator('tr').last().waitFor();
      assert.equal(reads.at(-1), null, 'Each opening resolves the default again');
      const contact = value('contact_person');
      assert.equal(await contact.getAttribute('role'), 'combobox');
      assert.equal(await value('email').inputValue(), 'anna@example.test');
      await value('company_name').fill('Keep this company draft');
      await contact.fill('zoe');
      assert.equal(await send.isEnabled(), false, 'Unconfirmed search text cannot be transmitted');
      await page.getByRole('option', {name: 'Zoe Example · zoe@example.test', exact: true}).click();
      assert.equal(await value('email').inputValue(), 'zoe@example.test');
      assert.equal(await value('email_link').inputValue(), 'mailto:zoe@example.test');
      assert.equal(await value('email_break').inputValue(), 'zoe@example.test');
      assert.equal(await value('company_name').inputValue(), 'Keep this company draft');
      await value('email').fill('manually-edited@example.test');
      await contact.fill('not a linked contact');
      await page.getByText('Keine passenden verknüpften Kontakte gefunden.', {exact: true}).waitFor();
      assert.equal(await send.isEnabled(), false);
      await contact.press('Escape');
      assert.equal(await contact.inputValue(), 'Zoe Example');
      assert.equal(await value('email').inputValue(), 'manually-edited@example.test');
      assert.equal(await page.locator('[role=dialog]').isVisible(), true, 'Escape closes suggestions before the drawer');
      await contact.click();
      await page.getByRole('option', {name: 'Zoe Example · zoe@example.test', exact: true}).click();
      assert.equal(await value('email').inputValue(), 'manually-edited@example.test', 'Reselecting the same contact preserves manual email');
      await contact.fill('other-zoe@');
      await contact.press('ArrowDown'); await contact.press('Enter');
      assert.equal(await value('email').inputValue(), 'other-zoe@example.test', 'Duplicate names are distinguished by ID and email');
      await contact.fill('Without');
      await contact.press('Enter');
      assert.equal(await value('email').inputValue(), '');
      assert.equal(await value('email_link').inputValue(), '');
      assert.equal(await rows.locator('input[value=email]').isChecked(), false);
      assert.equal(await rows.locator('input[value=email]').isEnabled(), false, 'Missing email never keeps the previous contact email');
      await contact.fill('Anna'); await contact.press('ArrowDown'); await contact.press('Enter');
      await value('email').fill('manual-final@example.test');
      await all.uncheck(); await rows.locator('input[value=email]').check();
      fail = 0; await send.click(); await page.locator('a[href="/wordpress/jobs/7"]').waitFor();
      assert.equal(sends.at(-1).get('contact_id'), '10');
      assert.deepEqual(JSON.parse(sends.at(-1).get('edited_values_json')), {email: 'manual-final@example.test'});
      const bounds = await page.locator('[role=dialog]').boundingBox();
      assert.ok(bounds.x >= -1 && bounds.x + bounds.width <= width + 1);
      await page.keyboard.press('Escape');
      assert.equal(sends.length, before + 1, 'Only the explicit send above, never search or cancellation, submits');
      rejectPreview = true; await open();
      await page.getByText('Keine Bridge-Verbindung.', {exact: true}).waitFor();
      assert.equal(await send.isEnabled(), false);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Desktop/mobile: contact search, duplicate names, missing email, editable values, selective payload, draft preservation, cancellation and uncertain outcomes passed.');
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});
