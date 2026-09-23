const assert = require('node:assert/strict');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

// Render the production partial and execute the actual workbench and policy scripts.
const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
const partial = execFileSync(python, ['-c', `
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('app/templates'), autoescape=True)
print(env.get_template('partials/plugin_auto_updates.html').render(csrf_token='test-token',
    plugin_options=[('elementor/elementor.php', 'Elementor'),
    ('elementor-pro/elementor-pro.php', 'Elementor Pro'), ('other/other.php', '<img onerror=alert(1)>')]))
`], {encoding: 'utf8'});
const workbench = [...fs.readFileSync('app/templates/updates.html', 'utf8')
  .matchAll(/<script>\s*([\s\S]*?)<\/script>/g)].find(match => match[1].includes('var autoFilterPanel'))[1]
  .replace('{{ site_selector.form_id }}', 'site-selector');
const policy = fs.readFileSync('app/static/plugin-auto-updates.js', 'utf8');

(async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHANNEL ? {channel: process.env.PLAYWRIGHT_CHANNEL} : {});
  try {
    for (const width of [1280, 390]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => route.fulfill({status: 200, body: '<!doctype html><html><body></body></html>'}));
      await page.goto('https://hub.test/updates');
      await page.setContent(`<style>[hidden]{display:none}dialog{max-width:calc(100vw - 48px)}label{display:block}</style>
        <form id="site-selector" data-site-selector>
          <label><input type="checkbox" name="site_id" value="11"><span>First site</span></label>
          <label><input type="checkbox" name="site_id" value="22"><span>Second site</span></label>
          <label><input type="checkbox" name="site_id" value="33"><span>Not selected</span></label>
        </form>
        <select data-update-action-select><option value="">Choose</option><option value="auto-updates">Plugin policy</option></select>
        <button type="button" data-update-action-start>Starten</button>
        ${partial.replace(/<script[\s\S]*?<\/script>/g, '')}`);
      await page.addScriptTag({content: workbench});
      await page.addScriptTag({content: policy});
      await page.evaluate(() => {
        window.submissions = [];
        document.addEventListener('submit', event => {
          if (event.defaultPrevented) return;
          event.preventDefault();
          window.submissions.push([...new FormData(event.target)]);
        });
      });
      const start = page.locator('[data-update-action-start]');
      const dialog = page.locator('[data-auto-update-confirmation]');
      await page.locator('[data-update-action-select]').selectOption('auto-updates');
      assert.equal(await start.isDisabled(), true);
      for (const id of ['11', '22']) await page.locator(`[data-site-selector] input[value="${id}"]`).check();
      await page.evaluate(() => document.dispatchEvent(new Event('site-selector-change')));
      assert.equal(await start.isDisabled(), true);
      for (const file of ['elementor/elementor.php', 'elementor-pro/elementor-pro.php', 'other/other.php']) {
        await page.locator(`input[name="plugin_file"][value="${file}"]`).check();
      }
      assert.equal(await start.isEnabled(), true);
      await start.click();
      assert.equal(await dialog.isVisible(), true);
      assert.equal(await dialog.locator('li').count(), 5);
      assert.equal(await dialog.locator('img').count(), 0);
      assert.match(await dialog.innerText(), /<img onerror=alert\(1\)>/);
      await page.locator('[data-auto-update-cancel]').click();
      assert.equal(await dialog.isVisible(), false);
      assert.deepEqual(await page.evaluate(() => window.submissions), []);
      await start.click();
      await page.locator('[data-auto-update-confirm]').click();
      const entries = await page.evaluate(() => window.submissions[0]);
      assert.deepEqual(entries.filter(([key]) => key === 'site_id').map(([, value]) => value), ['11', '22']);
      assert.equal(entries.filter(([key]) => key === 'plugin_file').length, 3);
      assert.equal(Object.fromEntries(entries).blocked, 'true');
      assert.equal(Object.fromEntries(entries).confirmed, 'yes');
      assert.equal(Object.fromEntries(entries).csrf_token, 'test-token');
      await page.locator('select[name="blocked"]').selectOption('false');
      await start.click();
      assert.match(await dialog.innerText(), /nicht eingeschaltet/);
      await page.locator('[data-auto-update-confirm]').click();
      assert.equal(await page.evaluate(() => Object.fromEntries(window.submissions[1]).blocked), 'false');
      for (const id of ['11', '22']) await page.locator(`[data-site-selector] input[value="${id}"]`).uncheck();
      await page.evaluate(() => document.dispatchEvent(new Event('site-selector-change')));
      assert.equal(await start.isDisabled(), true);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Plugin policy browser checks passed (desktop and mobile).');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
