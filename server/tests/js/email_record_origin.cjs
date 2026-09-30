const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function extract(name) {
  const source = fs.readFileSync('app/templates/base.html', 'utf8');
  const match = source.match(new RegExp('^([ \\t]*)function ' + name + '\\([^\\n]*\\) \\{[\\s\\S]*?^\\1\\}', 'm'));
  assert.ok(match, name);
  return match[0];
}

function context(originCustomerId, leadId) {
  const values = {
    globalMailboxOriginCustomerId: originCustomerId,
    globalMailboxLeadId: {value: leadId},
    globalMailboxRecipient: {value: ''},
    globalMailboxRecipientKey: {value: 'old'},
    globalMailboxRecipientCustomerId: {value: 'old'},
    globalMailboxRecipientName: {value: 'old'},
    globalMailboxForm: {action: '/emails/send'},
    globalMailboxComposer: {dataset: {}},
    globalMailboxTemplate: {value: ''},
    globalMailboxClearRecipientResults() {},
    globalMailboxStatusMessage() {},
    globalMailboxUpdateSubmit() {},
    globalMailboxScheduleDraftSave() {},
    encodeURIComponent,
  };
  vm.createContext(values);
  vm.runInContext(extract('globalMailboxClearRecipient') + '\n' + extract('globalMailboxSelectRecipient'), values);
  return values;
}

const customer = context('411', '');
customer.globalMailboxClearRecipient();
assert.equal(customer.globalMailboxForm.action, '/customers/411/communications/emails');
assert.equal(customer.globalMailboxRecipientCustomerId.value, '411');
customer.globalMailboxSelectRecipient({key: 'contact:7', customer_id: 412, email: 'other@example.test', name: 'Other'});
assert.equal(customer.globalMailboxForm.action, '/customers/411/communications/emails');
assert.equal(customer.globalMailboxRecipientCustomerId.value, '411');
assert.equal(customer.globalMailboxRecipientKey.value, '');

const lead = context('', '1804');
lead.globalMailboxSelectRecipient({key: 'contact:7', customer_id: 412, email: 'lead@example.test', name: 'Lead'});
assert.equal(lead.globalMailboxForm.action, '/emails/send');
assert.equal(lead.globalMailboxRecipientCustomerId.value, '');
assert.equal(lead.globalMailboxRecipientKey.value, '');
