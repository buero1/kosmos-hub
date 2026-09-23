const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const template = fs.readFileSync('app/templates/base.html', 'utf8');
const source = template.split('function customerActivitySetCreation(opener) {')[1].split('function customerActivityOpen(')[0];
const author = {textContent: ''};
const time = {textContent: ''};
const panel = {hidden: true, querySelectorAll: () => [], querySelector: selector =>
  selector === '[data-activity-creation-author]' ? author : selector === '[data-activity-creation-time]' ? time : {textContent: ''}};
const context = {customerActivityComposer: {querySelector: () => panel}};
vm.createContext(context);
vm.runInContext('function customerActivitySetCreation(opener) {' + source, context);
const update = context.customerActivitySetCreation;

update({closest: () => ({dataset: {activityCreatedBy: 'Sarah Muster', activityCreatedAt: '22.09.2026 09:12:13'}})});
assert.equal(panel.hidden, false);
assert.equal(author.textContent, 'Sarah Muster');
assert.equal(time.textContent, '22.09.2026 09:12:13');
update({dataset: {activityCreatedBy: 'Calendar Author', activityCreatedAt: '21.09.2026 12:00:00'}});
assert.equal(author.textContent, 'Calendar Author');
update(null);
assert.equal(panel.hidden, true);
assert.equal(author.textContent, '');
assert.equal(time.textContent, '');
update({closest: () => null});
assert.equal(panel.hidden, true);
update({dataset: {activityCreatedBy: '<script>alert(1)</script>'}});
assert.equal(author.textContent, '<script>alert(1)</script>');
assert.equal(time.textContent, '-');
for (const kind of ['Call', 'Task', 'Meeting']) {
  assert.ok(template.includes('function customer' + kind + 'OpenForEdit(opener) {\n            customerActivityOpen("' + kind.toLowerCase() + '");\n            customerActivitySetCreation(opener);'));
}
console.log('Activity creation drawer: opening, switching, calendar, clearing and text safety passed.');
