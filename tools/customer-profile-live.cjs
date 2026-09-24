// Live Hub rendering only. Every profile-send request is intercepted and rejected.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const base = 'https://kosmos-hub.31-70-92-95.sslip.io';
const customerId = process.argv[2] || '8';
assert.match(customerId, /^[1-9][0-9]*$/);
const auth = [
  "import os,sys,json,base64",
  "from dotenv import load_dotenv",
  "from itsdangerous import TimestampSigner",
  "os.chdir('/opt/kosmos-hub/app/server');sys.path.insert(0,os.getcwd())",
  "load_dotenv('/etc/kosmos-hub/kosmos-hub.env')",
  "from sqlalchemy import select,text",
  "from app.db.base import Base",
  "from app.db.session import SessionLocal",
  "from app.models.hub_user import HubUser",
  "from app.core.config import get_settings",
  "with SessionLocal() as db:",
  " db.execute(text('SET TRANSACTION READ ONLY'))",
  " user=db.scalar(select(HubUser).where(HubUser.role=='admin',HubUser.is_active.is_(True)))",
  " data=base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())",
  " print(TimestampSigner(get_settings().app_secret_key).sign(data).decode())",
].join('\n');
(async () => {
  const cookie = execFileSync('ssh', ['-o','ConnectTimeout=15','-i','C:/Users/User/.ssh/kosmos_api_root_ed25519',
    'root@31.70.92.95','/opt/kosmos-hub/venv/bin/python','-'], {input: auth, encoding: 'utf8', timeout: 90000}).trim();
  const browser = await chromium.launch({headless:true, channel:'chrome'});
  try {
    const context = await browser.newContext({viewport:{width:1300,height:912}});
    await context.addCookies([{name:'kosmos_hub_session', value:cookie, url:base, secure:true, httpOnly:true}]);
    const page = await context.newPage();
    page.setDefaultTimeout(45000);
    let writes=0, contactEmailPrefilled=false; const errors=[];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/website-profile/send', route => {writes++; return route.abort();});
    await page.goto(base+'/customers/'+customerId, {waitUntil:'domcontentloaded'});
    assert.equal(await page.getByText('Options an WP senden', {exact:true}).count(), 0);
    const button=page.locator('[data-website-profile-open]');
    await button.locator('xpath=ancestor::details/summary').click();
    const pending=page.waitForResponse(response=>response.url().includes('/website-profile/preview'));
    await button.click();
    const result=await pending;
    assert.ok([200,409].includes(result.status()), 'Preview must respond safely');
    const drawer=page.locator('.website-profile-drawer');
    assert.equal(await drawer.isVisible(), true);
    const bounds=await drawer.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x < 1300 && bounds.x+bounds.width <=1301);
    if(result.status()===409) {
      await page.locator('[data-website-profile-error]:not([hidden])').waitFor();
      assert.equal(await page.locator('[data-website-profile-send]').isEnabled(), false);
    } else {
      const data = await result.json();
      const values = Object.fromEntries(data.rows.map(row => [row.id, row]));
      const editable = id => page.locator('[data-field-id="'+id+'"]');
      await editable('company_name').waitFor();
      assert.equal(values.legal_name.proposed, values.company_name.proposed);
      const address = values.field_dddda53799ea71160555d834 || values.company_name_address;
      assert.ok(address.proposed.startsWith(values.company_name.proposed+', '));
      assert.ok(!address.proposed.startsWith('null,'));
      assert.equal(await editable('legal_name').inputValue(), values.company_name.proposed);
      assert.equal(await editable('contact_person').inputValue(), values.contact_person.proposed);
      assert.equal(await editable('contact_person').isEditable(), true);
      assert.equal(await editable('contact_person').getAttribute('role'), 'combobox');
      const contact = data.contacts.find(item => item.id === data.contact_id);
      assert.ok(contact, 'Default linked contact exists');
      assert.equal(values.email.proposed, contact.values.email);
      assert.equal(await editable('email').inputValue(), contact.values.email);
      contactEmailPrefilled = !!contact.email && contact.values.email === contact.email;
      await editable('contact_person').fill(contact.name.slice(0, 4));
      await page.getByRole('option').filter({hasText: contact.email || contact.name}).first().click();
      assert.equal(await editable('contact_person').inputValue(), contact.name);
      await editable('email').fill('local-draft@example.test');
      assert.equal(await editable('email').inputValue(), 'local-draft@example.test');
      await editable('email').fill(contact.values.email);
      await editable('legal_name').fill('Local draft only, never transmitted');
      assert.equal(await page.locator('input[type=checkbox][value=legal_name]').isChecked(), true);
      // Restore the visible suggestion for the screenshot; no send button is pressed.
      await editable('legal_name').fill(values.company_name.proposed);
    }
    const out=path.resolve(__dirname,'../server/outputs');
    fs.mkdirSync(out,{recursive:true});
    await page.screenshot({path:path.join(out,'customer-profile-live.png')});
    await page.getByRole('button',{name:'Abbrechen',exact:true}).click();
    assert.equal(await drawer.isVisible(), false);
    await page.setViewportSize({width:390,height:844});
    await button.locator('xpath=ancestor::details/summary').click();
    await button.click();
    await page.waitForTimeout(250);
    const mobile=await drawer.boundingBox();
    assert.ok(mobile.x >= -1 && mobile.x+mobile.width <=391);
    await page.keyboard.press('Escape');
    assert.equal(writes,0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({customer_id:Number(customerId),menu:true,retired_field_hidden:true,preview_status:result.status(),editable_values:result.status()===200,contact_email_prefilled:contactEmailPrefilled,desktop:true,mobile:true,cancel_writes:0}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
