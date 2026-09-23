const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function extract(path, name) {
  const source = fs.readFileSync(path, 'utf8');
  const match = source.match(new RegExp('^([ \\t]*)function ' + name + '\\([^\\n]*\\) \\{[\\s\\S]*?^\\1\\}', 'm'));
  assert.ok(match, name);
  return match[0];
}

async function check(globalComposer, scenario) {
  const prefix = globalComposer ? 'globalMailbox' : 'mailboxComposer';
  const save = globalComposer ? 'saveGlobalMailboxDraft' : 'saveMailboxDraft';
  const path = globalComposer ? 'app/templates/base.html' : 'app/templates/emails.html';
  const classes = new Set(['is-open']);
  const layer = {hidden: false, classList: {remove: key => classes.delete(key), contains: key => classes.has(key)}};
  const drawer = {setAttribute() {}};
  let resolve;
  let calls = 0;
  let errorShown = false;
  const context = {
    scopedMailboxUrl: value => new URL(value, 'https://hub.example.test'),
    window: {fetch: () => { calls++; return new Promise(done => { resolve = done; }); }, setTimeout: fn => fn()},
    document: {body: {classList: {remove() {}}}, querySelector: () => null},
    FormData: class {delete() {}}, Event: class {constructor(type) { this.type = type; }},
    folderCache: new Map(), mailboxFolderKey: (folder, unread) => folder + unread,
    mailboxStatus: {}, emailComposerLayer: layer, emailComposerDrawer: drawer, emailComposerTrigger: null,
    updateMailboxFolderCounts() {}, updateUnreadEmailAlert() {},
  };
  Object.assign(context, {
    [prefix + 'Form']: {}, [prefix + 'Submitting']: false,
    [prefix + 'DraftSavePromise']: null, [prefix + 'DraftSession']: 1, [prefix + 'DraftDirty']: false,
    [prefix + 'DraftStartedAt']: 0, [prefix + 'DraftId']: {value: ''},
    [prefix + 'ScheduledEmailId']: {value: ''}, [prefix + 'SaveDraft']: {disabled: false},
    [prefix + 'Attachments']: [], [prefix + 'StoredAttachments']: [],
    [globalComposer ? 'globalMailboxSetAttachments' : 'setMailboxComposerAttachments']: () => {},
    [globalComposer ? 'globalMailboxHasDraftContent' : 'mailboxComposerHasDraftContent']: () => true,
    [globalComposer ? 'globalMailboxClearDraftTimer' : 'clearMailboxComposerDraftTimer']: () => {},
    [globalComposer ? 'globalMailboxScheduleDraftSave' : 'scheduleMailboxComposerDraftSave']: () => {},
    [globalComposer ? 'globalMailboxStatusMessage' : 'setMailboxComposerStatus']: (_message, isError) => { errorShown = Boolean(isError); },
    globalMailboxComposer: layer, globalMailboxDrawer: drawer, mailboxComposer: layer,
  });
  layer.dispatchEvent = event => {
    assert.equal(event.type, 'email-compose-saved');
    context.closeEmailComposer();
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('app/static/email-draft-attachments.js', 'utf8'), context);
  vm.runInContext(extract(path, save) + '\n' + extract('app/templates/base.html', globalComposer ? 'globalMailboxClose' : 'closeEmailComposer'), context);
  const options = scenario === 'autosave' ? {automatic: true}
    : scenario === 'before-send' ? {force: true, quiet: true, beforeSend: true}
    : {force: true, closeAfterSave: true};
  const pending = context[save](options);
  assert.equal(classes.has('is-open'), true, 'Wait for the server before closing');
  if (scenario === 'new-session') context[prefix + 'DraftSession']++;
  if (scenario === 'edited-during-save') context[prefix + 'DraftDirty'] = true;
  resolve({ok: scenario !== 'failure', json: async () => scenario === 'failure'
    ? {detail: 'Save failed'} : scenario === 'invalid-response' ? {} : {draft_id: 31, attachments: [], uploaded_attachment_ids: []}});
  await pending;
  assert.equal(calls, 1, 'Closing after a save must not issue another save');
  assert.equal(classes.has('is-open'), scenario !== 'manual');
  assert.equal(layer.hidden, scenario === 'manual');
  assert.equal(errorShown, ['failure', 'invalid-response'].includes(scenario));
  if (scenario === 'manual') assert.equal(context[prefix + 'DraftId'].value, '31');
}

(async () => {
  assert.ok(fs.readFileSync('app/templates/base.html', 'utf8').includes('emailComposerLayer.addEventListener("email-compose-saved", closeEmailComposer)'));
  for (const globalComposer of [false, true]) {
    for (const scenario of ['manual', 'autosave', 'before-send', 'failure', 'invalid-response', 'new-session', 'edited-during-save']) {
      await check(globalComposer, scenario);
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
