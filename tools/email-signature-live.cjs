// Verify the editor and actor-bound rendering; --configure changes only the approved closing line.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const base = 'https://kosmos-hub.31-70-92-95.sslip.io';
const auth = [
  'import os,sys,json,base64,re',
  'from dotenv import load_dotenv',
  'from itsdangerous import TimestampSigner',
  "os.chdir('/opt/kosmos-hub/app/server');sys.path.insert(0,os.getcwd())",
  "load_dotenv('/etc/kosmos-hub/kosmos-hub.env')",
  'from sqlalchemy import select,text',
  'from app.db.base import Base',
  'from app.db.session import SessionLocal',
  'from app.models.hub_user import HubUser',
  'from app.models.zoho_email_template import ZohoEmailTemplate',
  'from app.core.config import get_settings',
  'from app.core.security import get_secret_cipher',
  'with SessionLocal() as db:',
  " db.execute(text('SET TRANSACTION READ ONLY'))",
  ' users=list(db.scalars(select(HubUser).where(HubUser.is_active.is_(True)).order_by(HubUser.id)))',
  " admin=next(u for u in users if u.role=='admin')",
  " staff=next((u for u in users if u.id==2 and u!=admin),None)",
  ' sessions=[]',
  ' for user in [admin]+([staff] if staff else []):',
  "  data=base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())",
  "  sessions.append({'name':user.display_name,'cookie':TimestampSigner(get_settings().app_secret_key).sign(data).decode()})",
  ' template_id=None',
  ' for row in db.scalars(select(ZohoEmailTemplate).where(ZohoEmailTemplate.is_active.is_(True))):',
  "  payload=json.loads(get_secret_cipher().decrypt(row.encrypted_payload_json))",
  "  if any(token in str(payload.get('content','')) for token in ('${Company.EmailSignature}','${userSignature}')):",
  '   template_id=row.zoho_template_id;break',
  " print(json.dumps({'sessions':sessions,'template_id':template_id}))",
].join('\n');

(async () => {
  const data = JSON.parse(execFileSync('ssh', ['-o', 'ConnectTimeout=15', '-i',
    'C:/Users/User/.ssh/kosmos_api_root_ed25519', 'root@31.70.92.95',
    '/opt/kosmos-hub/venv/bin/python', '-'], {input: auth, encoding: 'utf8', timeout: 90000}));
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  const cookie = value => ({name: 'kosmos_hub_session', value, url: base, secure: true, httpOnly: true});
  try {
    const context = await browser.newContext();
    await context.addCookies([cookie(data.sessions[0].cookie)]);
    let writes = 0, configured = false;
    for (const width of [1263, 390]) {
      const page = await context.newPage();
      await page.setViewportSize({width, height: 912});
      page.setDefaultTimeout(45000);
      await page.route('**/*', route => {
        if (!['GET', 'HEAD'].includes(route.request().method())) {writes++; return route.abort();}
        return route.continue();
      });
      await page.goto(base + '/settings#account-mailbox', {waitUntil: 'domcontentloaded'});
      const form = page.locator('form[action="/account/mail-signature"]');
      const details = form.locator('xpath=ancestor::details').first();
      if (!(await details.getAttribute('open') !== null)) await details.locator('summary').click();
      const original = await form.locator('[name="signature_html"]').inputValue();
      const editor = form.frameLocator('.jodit-wysiwyg_iframe').locator('body');
      await editor.waitFor({state: 'visible'});
      for (const [label, token] of [['Vollständiger Name', '${User.Name}'], ['Vorname', '${User.FirstName}'], ['Nachname', '${User.LastName}']]) {
        await form.locator('[data-template-placeholder-search]').fill(label);
        const option = form.locator('[data-template-placeholder-option]').filter({hasText: token});
        assert.equal(await option.isVisible(), true);
        await option.click();
        assert.ok((await editor.textContent()).includes(token));
      }
      if (width === 1263 && process.argv.includes('--configure') && !original.includes('${User.Name}')) {
        assert.equal(original.split('Ihr Kosmos Team').length - 1, 1, 'Closing line must match exactly once');
        const target = original.replace('Ihr Kosmos Team', '${User.Name}');
        const output = path.resolve(__dirname, '../server/outputs');
        fs.mkdirSync(output, {recursive: true});
        fs.writeFileSync(path.join(output, 'signature-before-user-placeholder-' + Date.now() + '.html'), original);
        const csrf = await form.locator('[name="csrf_token"]').inputValue();
        // Use the authenticated, CSRF-protected settings route, not a direct DB write.
        const saved = await context.request.post(base + '/account/mail-signature', {
          form: {signature_html: target, csrf_token: csrf}, maxRedirects: 0,
        });
        assert.equal(saved.status(), 303);
        assert.ok(saved.headers().location.includes('email_signature=saved'));
        configured = true;
      }
      await page.close();
    }
    assert.equal(writes, 0, 'Browser must not send email or save its test insertions');
    assert.ok(data.template_id, 'An existing template must reference the shared signature');
    for (const user of data.sessions) {
      const personal = await browser.newContext();
      await personal.addCookies([cookie(user.cookie)]);
      const result = await personal.request.get(base + '/emails/compose/templates/' + encodeURIComponent(data.template_id));
      assert.equal(result.status(), 200);
      const rendered = await result.json();
      assert.ok(rendered.content.includes(user.name), 'Authenticated author must appear in composed signature');
      assert.ok(!/\$\{User\.(Name|FirstName|LastName)\}/.test(rendered.content));
      await personal.close();
    }
    console.log(JSON.stringify({desktop: true, mobile: true, placeholders: 3, author_previews: data.sessions.length,
      signature_configured: configured, email_sends: 0}));
  } finally {await browser.close();}
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
