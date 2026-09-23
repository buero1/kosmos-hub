const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(path, name, prefix) {
  const source = fs.readFileSync(path, 'utf8');
  const match = source.match(new RegExp('^([ \\t]*)function ' + name + '\\([^\\n]*\\) \\{[\\s\\S]*?^\\1\\}', 'm'));
  assert.ok(match, name);
  const pending = [];
  const context = {URLSearchParams, window: {fetch(url) {
    return new Promise(resolve => pending.push({url, resolve}));
  }}};
  for (const symbol of new Set(match[0].match(new RegExp('\\b' + prefix + '[A-Z]\\w*', 'g')))) {
    context[symbol] = {value: '', dataset: {}};
  }
  for (const symbol of ['globalMailboxStatusMessage', 'globalMailboxSetContent', 'globalMailboxSyncContent',
    'globalMailboxScheduleDraftSave', 'setMailboxComposerStatus', 'syncMailboxComposerContent', 'scheduleMailboxComposerDraftSave']) {
    context[symbol] = () => {};
  }
  context[prefix + 'Content'] = null;
  context[prefix + 'TemplateRequest'] = 0;
  context[prefix + 'Template'].value = 'source';
  context[prefix + 'ContextModule'].value = 'offers';
  context[prefix + 'ContextRecordId'].value = '77';
  context[prefix + 'LeadId'].value = '42';
  context[prefix + 'Recipient'].value = 'lea@example.test';
  if (prefix === 'globalMailbox') context.globalMailboxContextPath = '/finance/offers/77';
  vm.createContext(context);
  vm.runInContext(match[0], context);
  return {context, pending, name, prefix};
}

(async () => {
  for (const args of [
    ['app/templates/base.html', 'globalMailboxLoadTemplate', 'globalMailbox'],
    ['app/templates/emails.html', 'loadMailboxComposerTemplate', 'mailboxComposer'],
  ]) {
    const {context, pending, name, prefix} = setup(...args);
    context[name]();
    const params = new URL(pending[0].url, 'https://hub.test').searchParams;
    assert.equal(params.get('context_module'), 'offers');
    assert.equal(params.get('context_record_id'), '77');
    assert.equal(params.get('lead_id'), '42');
    assert.equal(params.get('recipient_email'), 'lea@example.test');
    context[name]();
    pending[1].resolve({ok: true, json: async () => ({subject: 'Newest', content: 'Body',
      template_context: {context_module: 'offers', context_record_id: '77', lead_id: '42'}})});
    await new Promise(setImmediate);
    pending[0].resolve({ok: true, json: async () => ({subject: 'Stale', template_context: {context_record_id: '99'}})});
    await new Promise(setImmediate);
    assert.equal(context[prefix + 'Subject'].value, 'Newest');
    assert.equal(context[prefix + 'ContextRecordId'].value, '77', 'Stale requests cannot replace the saved source');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
