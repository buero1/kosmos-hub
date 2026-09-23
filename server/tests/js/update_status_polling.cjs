const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../../app/templates/updates.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).filter(script => script.includes('window.fetch(container.dataset.statusUrl'));
assert.equal(scripts.length, 3);

(async () => {
  for (const script of scripts) {
    let poll, resolve, reject, requests = 0, cleared = false;
    const elements = new Map();
    const container = {
      dataset: {statusUrl: '/test-status', refreshMode: 'fresh-updates'},
      querySelector(selector) {
        if (selector.includes('rows') || selector.includes('events')) return null;
        if (!elements.has(selector)) elements.set(selector, {style: {}, setAttribute() {}});
        return elements.get(selector);
      },
    };
    const window = {
      setInterval(callback) { poll = callback; return 1; },
      clearInterval() { cleared = true; },
      fetch() {
        requests += 1;
        return new Promise((ok, fail) => { resolve = ok; reject = fail; });
      },
    };
    vm.runInNewContext(script, {window, document: {querySelector: () => container}});
    assert.equal(requests, 1);
    for (let i = 0; i < 30; i++) poll();
    assert.equal(requests, 1, 'Slow requests must not accumulate');
    reject(new Error('temporary network error'));
    await new Promise(setImmediate);
    poll();
    assert.equal(requests, 2, 'Network errors must allow retry');
    resolve({ok: false});
    await new Promise(setImmediate);
    poll();
    assert.equal(requests, 3, 'HTTP errors must allow retry');
    resolve({ok: true, json: async () => ({status: 'running', completed: 0, total: 1,
      result: {sites: {completed: 0, total: 1}, phase: {completed: 0, total: 1}}})});
    await new Promise(setImmediate);
    assert.equal(cleared, false);
    poll();
    assert.equal(requests, 4, 'Successful intermediate responses must allow retry');
    resolve({ok: true, json: async () => ({status: 'succeeded', completed: 1, total: 1,
      result: {sites: {completed: 1, total: 1}, phase: {completed: 1, total: 1}}})});
    await new Promise(setImmediate);
    assert.equal(cleared, true, 'Completed runs must stop polling');
  }
  console.log('All 3 workbench pollers: slow requests, network/HTTP recovery, completion passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
