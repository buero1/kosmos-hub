const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/templates/emails.html', 'utf8');
const names = ['scopedMailboxUrl', 'mailboxFolderKey', 'mailboxFolderUrl', 'mailboxMessageUrl'];
const functions = names.map(name => {
  const match = source.match(new RegExp('^([ \\t]*)function ' + name + '\\([^\\n]*\\) \\{[\\s\\S]*?^\\1\\}', 'm'));
  assert.ok(match, name);
  return match[0];
}).join('\n');
const context = {URL, mailboxAccountId: '3', window: {location: {origin: 'https://hub.test'}}};
vm.createContext(context);
vm.runInContext(functions, context);
for (const path of ['/emails/list', '/emails/status', '/emails/actions', '/emails/drafts', '/emails/compose/options']) {
  assert.equal(context.scopedMailboxUrl(path).searchParams.get('account_id'), '3');
}
const key3 = context.mailboxFolderKey('inbox', false);
const folder = new URL(context.mailboxFolderUrl('drafts', true));
assert.equal(folder.searchParams.get('account_id'), '3');
assert.equal(folder.searchParams.get('unread'), 'true');
const message = new URL(context.mailboxMessageUrl({dataset: {mailboxFolderName: 'sent'}}, 'unassigned-9'));
assert.equal(message.searchParams.get('account_id'), '3');
assert.equal(message.searchParams.get('selected'), 'unassigned-9');
context.mailboxAccountId = '1';
assert.notEqual(context.mailboxFolderKey('inbox', false), key3);
context.mailboxAccountId = '';
assert.equal(context.scopedMailboxUrl('/emails').searchParams.has('account_id'), false);
console.log('Account-scoped navigation, request URLs and cache keys verified.');
