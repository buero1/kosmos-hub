// Explicitly scoped upgrade QA. One-time launch URLs, secrets and cookies are never logged/saved.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const assert = require('node:assert/strict');
const siteId = Number(process.argv[2]);
const allowed = {2: 'test-gasthofloewen.kosmos-medien.de', 14: 'baeckerei-glas.de'};
assert.ok(allowed[siteId], 'Only dedicated test site 2 or affected site 14 allowed.');
const base = 'https://' + allowed[siteId];
const version = '0.3.69';
const boot = `
import os,sys,json,hashlib
from dotenv import load_dotenv
os.chdir('/opt/kosmos-hub/app/server');sys.path.insert(0,os.getcwd());load_dotenv('/etc/kosmos-hub/kosmos-hub.env')
from sqlalchemy import select,func
from app.db.base import Base
from app.db.session import SessionLocal
from app.core.security import get_secret_cipher
from app.models.site import Site
from app.services.site_admin_launch import SiteAdminLaunchService
`;
function remote(script) {
  return JSON.parse(execFileSync('ssh',['-o','ConnectTimeout=15','-i','C:/Users/User/.ssh/kosmos_api_root_ed25519',
    'root@31.70.92.95','/opt/kosmos-hub/venv/bin/python','-'],{input:boot+script,encoding:'utf8',timeout:120000}));
}
function snapshot() {
  return remote(`
with SessionLocal() as db:
 s=db.get(Site,${siteId});assert s.domain==${JSON.stringify(allowed[siteId])}
 cipher=get_secret_cipher()
 print(json.dumps({'id':s.id,'uuid':s.uuid,'customer_id':s.customer_id,'version':s.bridge_version,
  'domain_count':db.scalar(select(func.count()).select_from(Site).where(Site.domain==s.domain)),
  'connections':[{'id':c.id,'provider':c.provider,'endpoint':c.endpoint,'key_hash':hashlib.sha256(cipher.decrypt(c.encrypted_credentials).encode()).hexdigest()} for c in s.connections]}))
`);
}
(async()=>{
  const before=snapshot();
  assert.equal(before.domain_count,1,'No duplicate site before upgrade');
  const launch=remote(`
with SessionLocal() as db:
 s=db.get(Site,${siteId});assert s.domain==${JSON.stringify(allowed[siteId])}
 launch=SiteAdminLaunchService(db=db,cipher=get_secret_cipher()).open_admin(site_id=s.id,actor='bridge-registration-fix-qa')
 print(json.dumps({'url':launch.launch_url}))
`);
  assert.equal(new URL(launch.url).origin,base);
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1400,height:950}});
  let page;
  try {
    page=await context.newPage();
    await page.goto(launch.url,{waitUntil:'domcontentloaded',timeout:90000});
    await page.waitForURL(base+'/wp-admin/**');
    if (process.argv.includes('--install')) {
      await page.goto(base+'/wp-admin/plugin-install.php?tab=upload');
      await page.locator('input[name=pluginzip]').setInputFiles(path.resolve(__dirname,'../dist/kosmos-bridge-'+version+'.zip'));
      await page.locator('#install-plugin-submit').click();
      const replace=page.locator('a[href*="overwrite=update-plugin"]');
      await replace.waitFor({timeout:120000});
      await replace.click();
      await page.waitForLoadState('domcontentloaded');
    }
    await page.goto(base+'/wp-admin/plugins.php',{waitUntil:'domcontentloaded',timeout:90000});
    const plugin=page.locator('[data-plugin="kosmos-bridge/kosmos-bridge.php"]');
    assert.ok((await plugin.innerText()).includes(version),'Expected Bridge version installed');
    assert.equal(await plugin.locator('a[href*="action=deactivate"]').count(),1,'Bridge active');
    await page.goto(base+'/wp-admin/tools.php?page=kosmos-bridge');
    const readRows=()=>page.locator('.wrap > table.widefat tbody tr').evaluateAll(nodes=>Object.fromEntries(nodes.map(n=>[
      n.querySelector('th')?.textContent.trim(),n.querySelector('td')?.textContent.trim()
    ])));
    let rows=await readRows();
    assert.equal(rows['Bridge version'],version);
    assert.equal(rows['Status'],'ok','Bound registration confirms identity after upgrade');
    assert.equal(rows['Site UUID'],before.uuid,'Upgrade must preserve UUID');
    assert.ok(rows['Last success'],'Current identity confirmed');
    // Independent PHP requests on the same domain must not rotate the identity.
    for (let i=0;i<4;i++) {
      const response=await context.request.get(base+'/?bridge_qa='+Date.now()+'-'+i);
      assert.equal(response.status(),200,'Public website healthy');
    }
    await page.reload();
    rows=await readRows();
    assert.equal(rows['Site UUID'],before.uuid,'Repeated requests preserve UUID');
    assert.equal(rows['Status'],'ok');
    const after=snapshot();
    assert.equal(after.version,version,'Hub learned current version');
    assert.deepEqual({...after,version:before.version},before,'Site/customer/connection/secret must stay unchanged');
    const bridge=remote(`
from app.services.site_mcp_proxy import SiteMcpProxyService
with SessionLocal() as db:
 result=SiteMcpProxyService(db=db,cipher=get_secret_cipher()).execute_readonly_ability(${siteId},'kosmos-bridge/get-environment-info',None)
 print(json.dumps(result['result']))
`);
    assert.equal(bridge.site_uuid,before.uuid,'Signed Hub request accepted after upgrade');
    assert.equal(bridge.bridge_version,version);
    await page.screenshot({path:path.resolve(__dirname,'../tmp/bridge-registration-'+siteId+'.png'),fullPage:true});
    console.log(JSON.stringify({site_id:siteId,domain:allowed[siteId],version,status:'ok',identity_and_customer_preserved:true,duplicate_created:false,public_site_healthy:true}));
  } finally {
    if (page) {
      const logout=await page.locator('#wp-admin-bar-logout a').getAttribute('href').catch(()=>null);
      if (logout && new URL(logout).origin===base) await page.goto(logout,{waitUntil:'domcontentloaded'}).catch(()=>{});
    }
    await context.close();
    await browser.close();
  }
})().catch(error=>{console.error(error.message.replace(/kosmos_admin_launch=[^\s"']+/g,'kosmos_admin_launch=[redacted]'));process.exitCode=1;});
