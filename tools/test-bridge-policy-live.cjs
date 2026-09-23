// Only the dedicated test installation is touched; authentication is never logged.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const base = 'https://test-gasthofloewen.kosmos-medien.de';
const state = path.join(os.tmpdir(), 'kosmos-content-kit-test-browser.json');

(async () => {
  const browser = await chromium.launch({channel: 'chrome'});
  try {
    const context = await browser.newContext(fs.existsSync(state) ? {storageState: state} : {});
    const page = await context.newPage();
    await page.goto(base + '/wp-admin/plugins.php');
    if (page.url().includes('wp-login.php')) {
      const launch = JSON.parse(execFileSync('ssh', ['-i', 'C:/Users/User/.ssh/kosmos_api_root_ed25519',
        'root@31.70.92.95', '/opt/kosmos-hub/venv/bin/python', '-'], {
        encoding: 'utf8', input: fs.readFileSync(path.join(root, 'tools/content-kit-test-launch.py'), 'utf8'),
      }));
      assert.equal(new URL(launch.url).origin, base);
      await page.goto(launch.url);
      await page.waitForURL('**/wp-admin/**');
    }
    if (process.argv.includes('--install')) {
      await page.goto(base + '/wp-admin/plugin-install.php?tab=upload');
      await page.locator('input[name=pluginzip]').setInputFiles(path.join(root, 'dist/kosmos-bridge-0.3.68.zip'));
      await page.locator('#install-plugin-submit').click();
      const replace = page.locator('a[href*="overwrite=update-plugin"]');
      await replace.waitFor({timeout: 120000});
      await replace.click();
      await page.waitForLoadState('domcontentloaded');
    }
    await page.goto(base + '/wp-admin/plugins.php');
    const row = page.locator('[data-plugin="kosmos-bridge/kosmos-bridge.php"]');
    assert.match(await row.innerText(), /0\.3\.68/);
    assert.equal(await row.locator('a[href*="action=deactivate"]').count(), 1);
    await context.storageState({path: state});
    console.log('Dedicated test site: Bridge 0.3.68 active; WordPress plugin admin renders.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
