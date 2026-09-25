const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function renderer() {
  const elements = new Map();
  const calls = [];
  const handlers = {};
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {
      hidden: false, checked: false, value: '', textContent: '', innerHTML: '',
      classList: {toggle() {}}, listeners: {},
      addEventListener(type, listener) { this.listeners[type] = listener; },
    });
    return elements.get(selector);
  }
  const api = {
    onUpdate(callback) { handlers.update = callback; },
    onError() {}, onRefresh() {},
    getState: async () => ({configured: true}),
    snooze: async (...args) => calls.push(['snooze', ...args]),
    snoozeBeforeStart: async (...args) => calls.push(['beforeStart', ...args]),
    complete: async (...args) => calls.push(['complete', ...args]),
    openHubPath: async (...args) => calls.push(['open', ...args]),
  };
  const context = vm.createContext({document: {querySelector: element}, window: {kosmosNotifier: api}, Intl, Date});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../renderer/renderer.js'), 'utf8'), context);
  return {
    update: reminders => handlers.update({reminders}), element, calls,
    ids: () => Array.from(vm.runInContext('selectedIds()', context)),
    toggle: id => vm.runInContext(`toggleReminderSelection(${id})`, context),
    click: selector => element(selector).listeners.click(),
  };
}

const reminder = (id = 1, extra = {}) => ({id, activity_name: 'Test reminder', activity_kind: 'task',
  activity_label: 'Aufgabe', activity_url: '/activities/task/7', starts_at: '2020-01-01T10:00:00Z',
  related_label: 'Kunde', related_url: '/customers/5', related_name: 'Example GmbH', ...extra});

test('one reminder is selected immediately and actions receive its id', () => {
  const ui = renderer();
  ui.update([reminder()]);
  assert.deepEqual(ui.ids(), [1]);
  assert.equal(ui.element('#select-all').checked, true);
  assert.match(ui.element('#reminder-list').innerHTML, /aria-pressed="true"/);
  assert.match(ui.element('#reminder-list').innerHTML, / checked>/);
  assert.equal(ui.element('#reminder-count').textContent, 'Test reminder');
  assert.equal(ui.element('#selected-reminder-appointment').hidden, false);
  ui.click('#bulk-snooze-button');
  ui.click('#bulk-complete-button');
  assert.deepEqual(JSON.parse(JSON.stringify(ui.calls)), [['snooze', [1], 10], ['complete', [1]]]);
});

test('deselection survives refresh and select-all can deselect a single reminder', () => {
  const ui = renderer();
  ui.update([reminder()]);
  ui.toggle(1);
  ui.update([reminder()]);
  assert.deepEqual(ui.ids(), []);
  ui.toggle(1);
  ui.element('#select-all').checked = false;
  ui.element('#select-all').listeners.change();
  ui.update([reminder()]);
  assert.deepEqual(ui.ids(), []);
});

test('multiple reminders keep existing choices; the last remaining reminder is selected', () => {
  const ui = renderer();
  ui.update([reminder(1), reminder(2)]);
  assert.deepEqual(ui.ids(), []);
  ui.toggle(1);
  ui.update([reminder(1), reminder(2)]);
  assert.deepEqual(ui.ids(), [1]);
  ui.update([reminder(2)]);
  assert.deepEqual(ui.ids(), [2]);
  ui.update([reminder(3)]);
  assert.deepEqual(ui.ids(), [3]);
  ui.update([]);
  assert.deepEqual(ui.ids(), []);
  assert.equal(ui.element('#empty-state').hidden, false);
  ui.update([reminder(3)]);
  assert.deepEqual(ui.ids(), [3]);
});

for (const [label, url, name] of [['Kunde', '/customers/5', 'Example GmbH'], ['Lead', '/leads/8', 'Erika Example']]) {
  test(`${label} name is the escaped link label and target is unchanged`, () => {
    const ui = renderer();
    ui.update([reminder(1, {related_label: label, related_url: url, related_name: name + ' & <img src=x>'})]);
    const html = ui.element('#reminder-list').innerHTML;
    assert.ok(html.includes(`data-hub-path="${url}">${name} &amp; &lt;img src=x&gt;</button>`));
    assert.ok(!html.includes(`${label} öffnen`));
    assert.ok(html.includes('data-hub-path="/activities/task/7">Aufgabe öffnen</button>'));
    ui.element('#reminder-list').listeners.click({
      target: {closest: selector => selector === '[data-hub-path]' ? {dataset: {hubPath: url}} : null}, stopPropagation() {},
    });
    assert.deepEqual(JSON.parse(JSON.stringify(ui.calls)), [['open', url]]);
    assert.deepEqual(ui.ids(), [1]);
  });
}

test('legacy customer payload, missing names, cases and unrelated activities retain safe links', () => {
  const ui = renderer();
  ui.update([reminder(1, {related_name: undefined, customer_name: 'Legacy customer'})]);
  assert.match(ui.element('#reminder-list').innerHTML, />Legacy customer<\/button>/);
  ui.update([reminder(1, {related_label: 'Lead', related_name: undefined, related_url: '/leads/8'})]);
  assert.match(ui.element('#reminder-list').innerHTML, />Lead öffnen<\/button>/);
  ui.update([reminder(1, {related_label: 'Fall', related_name: null, related_url: '/cases/9'})]);
  assert.match(ui.element('#reminder-list').innerHTML, />Fall öffnen<\/button>/);
  ui.update([reminder(1, {related_label: null, related_name: null, related_url: null})]);
  assert.ok(!ui.element('#reminder-list').innerHTML.includes('reminder-related-link'));
});
