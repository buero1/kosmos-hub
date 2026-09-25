const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Execute the actual composer functions with lightweight DOM fields, without a browser dependency.
function loadFunctions(path, names) {
  const source = fs.readFileSync(path, 'utf8');
  const functions = names.map(name => {
    const match = source.match(new RegExp('^([ \\t]*)function ' + name + '\\([^\\n]*\\) \\{[\\s\\S]*?^\\1\\}', 'm'));
    assert.ok(match, name);
    return match[0];
  }).join('\n');
  const context = {window: {setTimeout() {}, location: {pathname: '/leads/42'}}, document: {body: {classList: {add() {}}}}, Event: class {}};
  for (const name of new Set(functions.match(/\b(?:globalMailbox|mailboxComposer)[A-Z]\w*/g))) {
    context[name] = functions.includes(name + '(') ? () => {} : {
      value: '', dataset: {}, options: [], classList: {add() {}},
      setAttribute() {}, focus() {}, replaceChildren() {}, dispatchEvent() {},
      closest() { return this.label || (this.label = {hidden: false}); },
    };
  }
  vm.createContext(context);
  vm.runInContext(functions, context);
  return context;
}

(async () => {
  for (const scenario of [
    {email: 'lea@example.test', leadId: '42', canSend: true, disabled: false},
    {email: 'customer@example.test', canSend: true, disabled: false},
    {email: 'lea@example.test', leadId: '42', canSend: false, disabled: true},
    {email: 'lea@example.test', leadId: '42', canSend: true, sender: '', disabled: true},
    {email: 'not-an-address', leadId: '42', canSend: true, disabled: true},
    {email: '', leadId: '42', canSend: true, disabled: true},
    {email: 'lea@example.test', leadId: '42', canSend: true, submitting: true, disabled: true},
  ]) {
    const composer = loadFunctions('app/templates/base.html', [
      'globalMailboxOpen', 'globalMailboxClearRecipient', 'globalMailboxUpdateSubmit',
    ]);
    composer.globalMailboxSubmitting = Boolean(scenario.submitting);
    composer.globalMailboxSender.value = scenario.sender === undefined ? 'sender@example.test' : scenario.sender;
    composer.globalMailboxSender.selectedOptions = [{dataset: {canSend: String(scenario.canSend)}}];
    Object.defineProperty(composer.globalMailboxRecipient, 'validity', {
      get: () => ({valid: /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(composer.globalMailboxRecipient.value)}),
    });
    composer.globalMailboxLoadOptions = () => {
      // Sender options arrive while the recipient is still empty, as on first opening.
      composer.globalMailboxUpdateSubmit();
      return Promise.resolve();
    };
    // Enabling a valid prefilled address must not depend on the recipient lookup response.
    composer.globalMailboxSearchRecipients = () => new Promise(() => {});
    composer.globalMailboxOpen({dataset: {emailComposeLeadId: scenario.leadId || '', recipientEmail: scenario.email}});
    await new Promise(setImmediate);
    assert.equal(composer.globalMailboxSubmit.disabled, scenario.disabled, JSON.stringify(scenario));
    assert.equal(composer.globalMailboxLeadId.value, scenario.leadId || '');
    assert.equal(composer.globalMailboxRecipient.value, scenario.email);
  }

  const global = loadFunctions('app/templates/base.html', [
    'globalMailboxResetAction', 'globalMailboxClearRecipient', 'globalMailboxOpen',
  ]);
  global.globalMailboxLoadOptions = () => Promise.resolve();
  global.globalMailboxResetAction();
  global.globalMailboxOpen({dataset: {emailComposeLeadId: '42', recipientEmail: 'lea@example.test'}});
  let selectExact;
  global.globalMailboxSearchRecipients = (_email, exact) => { selectExact = exact; };
  await new Promise(setImmediate);
  assert.equal(global.globalMailboxLeadId.value, '42');
  assert.equal(global.globalMailboxRecipient.value, 'lea@example.test');
  assert.equal(selectExact, false, 'Do not silently replace a lead with a matching customer');
  global.globalMailboxClearRecipient();
  assert.equal(global.globalMailboxLeadId.value, '42', 'Editing the address keeps the source lead');
  global.globalMailboxResetAction();
  assert.equal(global.globalMailboxLeadId.value, '', 'New messages must not inherit a previous lead');
  global.globalMailboxOpen({dataset: {recipientEmail: 'customer@example.test'}});
  await new Promise(setImmediate);
  assert.equal(selectExact, true, 'Existing customer lookup behavior stays unchanged');

  const mailbox = loadFunctions('app/templates/emails.html', [
    'clearMailboxComposerActionFields', 'clearMailboxComposerRecipientContext', 'applyMailboxComposerPendingAction',
  ]);
  mailbox.mailboxComposer = {dataset: {}};
  mailbox.mailboxComposerLoaded = true;
  mailbox.mailboxComposerTemplates = [];
  for (const name of [
    'setMailboxComposerAttachments', 'clearMailboxComposerTemplateSelection',
    'clearMailboxComposerRecipientResults', 'updateMailboxComposerSubmit', 'setMailboxComposerStatus',
  ]) mailbox[name] = () => {};
  mailbox.mailboxComposerPendingAction = {action: 'draft', draft_id: 12, lead_id: 42, recipient_email: 'lea@example.test', context_module: 'offers', context_record_id: '77'};
  mailbox.applyMailboxComposerPendingAction();
  assert.equal(mailbox.mailboxComposerLeadId.value, 42);
  assert.equal(mailbox.mailboxComposerDraftId.value, 12);
  assert.equal(mailbox.mailboxComposerContextModule.value, 'offers');
  assert.equal(mailbox.mailboxComposerContextRecordId.value, '77');
  mailbox.clearMailboxComposerRecipientContext();
  assert.equal(mailbox.mailboxComposerLeadId.value, 42);
  mailbox.mailboxComposerPendingAction = {action: 'draft', draft_id: 13, recipient_email: 'other@example.test'};
  mailbox.applyMailboxComposerPendingAction();
  assert.equal(mailbox.mailboxComposerLeadId.value, '', 'An unrelated draft has no stale lead');
  assert.equal(mailbox.mailboxComposerContextRecordId.value, '', 'An unrelated draft has no stale template context');
  mailbox.mailboxComposerPendingAction = {action: 'draft', draft_id: 14, invoice_id: 5};
  mailbox.applyMailboxComposerPendingAction();
  assert.equal(mailbox.mailboxComposerScheduledAt.closest('label').hidden, true);
  mailbox.mailboxComposerLeadId.value = 42;
  mailbox.clearMailboxComposerActionFields();
  assert.equal(mailbox.mailboxComposerLeadId.value, '');
  assert.equal(mailbox.mailboxComposerScheduledAt.closest('label').hidden, false, 'Normal mail keeps scheduling');
})().catch(error => { console.error(error); process.exitCode = 1; });
