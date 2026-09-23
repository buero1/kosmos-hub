const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'app/templates/base.html'), 'utf8');
const markup = source.slice(source.indexOf('<button class="agent-launcher"'),
  source.indexOf('{% if can_use_global_email_composer and', source.indexOf('<button class="agent-launcher"')))
  .replace(/{% if agent_page_context %}[\s\S]*?{% endif %}/g, '');
const scriptStart = source.indexOf('(function setupHubAgentFloat()');
const scriptEnd = source.indexOf('        }());', scriptStart) + '        }());'.length;
const script = source.slice(scriptStart, scriptEnd).replace('{{ csrf_token | tojson }}', '"test-csrf"');
const venvPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const css = execFileSync(fs.existsSync(venvPython) ? venvPython : 'python', ['-c',
  'import sys; from jinja2 import Environment; from app.services.styling_settings import StylingRuntimeSettings; ' +
  'print(Environment().from_string(sys.stdin.read()).render(styling=StylingRuntimeSettings()))'
], {cwd: root, input: source.match(/<style>([\s\S]*?)<\/style>/)[1], encoding: 'utf8'});
const html = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
  '<style>' + css + '</style></head><body>' + markup + '<script>' + script + '</script></body></html>';
const payload = {
  csrf_token: 'test-csrf', conversation: {id: 1, title: 'Test conversation', status: 'active'},
  conversations: [{id: 1, title: 'Test conversation', status: 'active'}], contexts: [], messages: []
};

(async () => {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    const page = await browser.newPage();
    const errors = [];
    const requests = [];
    const requestDetails = [];
    let pending = null;
    page.on('pageerror', error => errors.push(error.message));
    // Every request stays local; in particular, no paid agent run is created.
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/' || url.pathname === '/with-context') return route.fulfill({contentType: 'text/html',
        body: (url.pathname === '/with-context' ? html.replace('data-agent-float hidden',
          'data-agent-float data-agent-page-context-type="customer" data-agent-page-context-key="123" hidden') : html)
          .replace('</body>', '<button data-agent-context-add data-agent-context-type="note" data-agent-context-key="456">Add context</button></body>')});
      if (!url.pathname.startsWith('/agent/chat')) return route.abort();
      requests.push(url.pathname + url.search);
      requestDetails.push({method: route.request().method(), data: route.request().postData()});
      const response = await new Promise(resolve => {pending = resolve;});
      pending = null;
      if (response === 'network') return route.abort();
      if (response === 'invalid') return route.fulfill({contentType: 'application/json', body: 'not json'});
      return route.fulfill({status: response.error ? 500 : 200, contentType: 'application/json', body: JSON.stringify(response)});
    });
    const working = page.locator('[data-agent-float-working]');
    const panel = page.locator('[data-agent-float]');
    const input = page.locator('[data-agent-float-input]');
    const send = page.locator('[data-agent-float-form] [type="submit"]');
    async function waitPending() {
      for (let retry = 0; !pending && retry < 100; retry++) await page.waitForTimeout(10);
      assert.ok(pending, 'Expected one deferred mock request');
      await working.waitFor({state: 'visible'});
      assert.equal(await input.isDisabled(), true);
      assert.equal(await send.isDisabled(), true);
      assert.equal(await page.locator('[data-agent-float-messages]').getAttribute('aria-busy'), 'true');
    }
    async function finish(response = payload) {
      pending(response);
      await working.waitFor({state: 'hidden'});
      assert.equal(await page.locator('[data-agent-float-messages]').getAttribute('aria-busy'), 'false');
      assert.equal(await input.isDisabled(), false);
      assert.equal(await send.isDisabled(), false);
    }
    for (const width of [1263, 390, 320]) {
      await page.setViewportSize({width, height: 912});
      await page.goto('http://hub.test/');
      assert.equal(await working.isVisible(), false);
      await page.locator('[data-agent-float-open]').click();
      await waitPending();
      assert.ok((await working.textContent()).includes('Neue Unterhaltung wird ge\u00f6ffnet'));
      assert.equal(requests.at(-1), '/agent/chat/new');
      assert.equal(requestDetails.at(-1).method, 'POST');
      assert.ok(requestDetails.at(-1).data.includes('csrf_token=test-csrf'));
      await finish();
      await input.fill('Read-only cost test');
      await send.click();
      await waitPending();
      assert.ok((await working.textContent()).includes('Hub-Agent arbeitet'));
      assert.equal(await working.locator('.agent-working-dots span').count(), 3);
      assert.equal(await working.getAttribute('role'), 'status');
      assert.equal(await working.locator('.agent-working-dots').getAttribute('aria-hidden'), 'true');
      const dots = await working.locator('.agent-working-dots span').evaluateAll(elements => elements.map(element => ({
        animation: getComputedStyle(element).animationName, delay: getComputedStyle(element).animationDelay,
        y: element.getBoundingClientRect().y
      })));
      assert.deepEqual(dots.map(dot => dot.animation), Array(3).fill('agent-working-dot'));
      assert.deepEqual(dots.map(dot => dot.delay), ['0s', '0.16s', '0.32s']);
      assert.ok(dots.every(dot => dot.y === dots[0].y), 'Dots share a horizontal row');
      const bounds = await working.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width);
      assert.ok(bounds.y + bounds.height <= 912);
      const count = requests.length;
      await page.locator('[data-agent-float-form]').evaluate(form => form.requestSubmit());
      await page.locator('[data-agent-float-new]').click();
      await page.locator('[data-agent-float-close]').click();
      assert.equal(await panel.isVisible(), false);
      await page.locator('[data-agent-float-open]').click();
      assert.equal(await working.isVisible(), true, 'Reopening must retain the in-flight indicator');
      assert.equal(requests.length, count, 'Busy actions must not issue overlapping requests');
      fs.mkdirSync(path.join(root, 'tmp'), {recursive: true});
      await page.screenshot({path: path.join(root, 'tmp/agent-working-' + width + '.png')});
      await finish({...payload, messages: [{instruction: 'Read-only cost test', response: 'Test complete.', actions: []}]});
      assert.ok((await page.locator('[data-agent-float-messages]').textContent()).includes('Test complete.'));
      await page.locator('[data-agent-float-close]').click();
      await page.locator('[data-agent-float-open]').click();
      await waitPending();
      assert.equal(requests.at(-1), '/agent/chat/new', 'An idle reopen starts fresh');
      assert.ok(!(await page.locator('[data-agent-float-messages]').textContent()).includes('Test complete.'),
        'The previous chat must not flash while starting a new one');
      await finish({...payload, conversation: {...payload.conversation, id: 2, title: 'New conversation'}});
      await page.locator('[data-agent-float-history] summary').click();
      await page.locator('[data-agent-float-conversation="1"]').click();
      await waitPending();
      assert.equal(requests.at(-1), '/agent/chat?conversation_id=1', 'History remains explicitly selectable');
      await finish({...payload, messages: [{instruction: 'Read-only cost test', response: 'Test complete.', actions: []}]});
    }
    for (const response of [{error: 'Test error'}, 'network', 'invalid']) {
      await input.fill('Failure test');
      await send.click();
      await waitPending();
      await finish(response);
      await page.locator('[data-agent-float-notice]').waitFor({state: 'visible'});
      assert.ok((await page.locator('[data-agent-float-messages]').textContent()).includes('Test complete.'),
        'Errors must not replace the existing chat');
    }
    await page.locator('[data-agent-float-upload]').setInputFiles({
      name: 'context.txt', mimeType: 'text/plain', buffer: Buffer.from('test context')
    });
    await waitPending();
    assert.ok((await working.textContent()).includes('Datei wird verarbeitet'));
    await page.emulateMedia({reducedMotion: 'reduce'});
    assert.deepEqual(await working.locator('.agent-working-dots span').evaluateAll(elements => elements.map(
      element => getComputedStyle(element).animationName)), ['none', 'none', 'none']);
    await finish();
    await page.goto('http://hub.test/with-context');
    await page.locator('[data-agent-float-open]').click();
    await waitPending();
    assert.equal(requests.at(-1), '/agent/chat/new');
    pending({...payload, conversation: {...payload.conversation, id: 3}});
    for (let retry = 0; requests.at(-1) !== '/agent/chat/context' && retry < 100; retry++) await page.waitForTimeout(10);
    await waitPending();
    assert.equal(requests.at(-1), '/agent/chat/context');
    let contextData = new URLSearchParams(requestDetails.at(-1).data);
    assert.equal(contextData.get('conversation_id'), '3');
    assert.equal(contextData.get('resource_key'), '123');
    assert.equal(contextData.get('resource_type'), 'customer');
    await finish({...payload, conversation: {...payload.conversation, id: 3}});
    await page.locator('[data-agent-float-close]').click();
    await page.locator('[data-agent-context-add]').click();
    await waitPending();
    assert.equal(requests.at(-1), '/agent/chat/new', 'Context button also starts fresh when the panel is closed');
    pending({...payload, conversation: {...payload.conversation, id: 4}});
    for (let retry = 0; requests.at(-1) !== '/agent/chat/context' && retry < 100; retry++) await page.waitForTimeout(10);
    await waitPending();
    const pageContextRequestCount = requests.length;
    pending({...payload, conversation: {...payload.conversation, id: 4}});
    for (let retry = 0; requests.length === pageContextRequestCount && retry < 100; retry++) await page.waitForTimeout(10);
    await waitPending();
    contextData = new URLSearchParams(requestDetails.at(-1).data);
    assert.equal(contextData.get('conversation_id'), '4', 'Explicit context must not fall back to an older matching conversation');
    assert.equal(contextData.get('resource_type'), 'note');
    assert.equal(contextData.get('resource_key'), '456');
    await finish({...payload, conversation: {...payload.conversation, id: 4}});
    assert.deepEqual(errors, []);
    console.log('Agent UI passed: desktop/mobile, new chat on open, explicit history, page/explicit context routing, busy reopen, success/errors, upload, duplicate guards and reduced motion. No live requests.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
