const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function extract(path, name) {
  const match = fs.readFileSync(path, 'utf8').match(new RegExp('^([ \\t]*)function ' + name + '\\([^\\n]*\\) \\{[\\s\\S]*?^\\1\\}', 'm'));
  assert.ok(match, name);
  return match[0];
}

function element(tagName) {
  return {
    tagName, value: '', events: {}, children: [], hidden: false, dataset: {},
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren() { this.children = []; },
    addEventListener(type, callback) { this.events[type] = callback; },
    setAttribute() {},
    querySelectorAll() {
      return this.children.flatMap(child => [
        ...(child.dataset.attachmentObjectUrl ? [child] : []), ...child.querySelectorAll(),
      ]);
    },
  };
}

function fixture(global) {
  const prefix = global ? 'globalMailbox' : 'mailboxComposer';
  const template = global ? 'app/templates/base.html' : 'app/templates/emails.html';
  const setter = global ? 'globalMailboxSetAttachments' : 'setMailboxComposerAttachments';
  const save = global ? 'saveGlobalMailboxDraft' : 'saveMailboxDraft';
  const hasContent = prefix + 'HasDraftContent';
  const field = element();
  const requests = [];
  const objectUrls = new Map();
  let objectUrlId = 0;
  let closeCount = 0;
  let error = false;
  const form = {elements: {namedItem: name => name === 'retained_attachment_ids' ? field : null}};
  const context = {
    scopedMailboxUrl: value => new URL(value, 'https://hub.example.test'),
    window: {
      fetch: (_url, options) => new Promise(resolve => requests.push({options, resolve})),
      URL: {
        createObjectURL: file => { const url = 'blob:attachment-' + ++objectUrlId; objectUrls.set(url, file); return url; },
        revokeObjectURL: url => objectUrls.delete(url),
      },
    },
    document: {createElement: element, querySelector: () => null},
    FormData: class {
      constructor() { this.fields = new Map([['retained_attachment_ids', field.value]]); }
      delete(key) { this.fields.delete(key); }
      append(key, value) { this.fields.set(key, [...(this.fields.get(key) || []), value]); }
    },
    DataTransfer: class {
      constructor() { this.files = []; this.items = {add: file => this.files.push(file)}; }
    },
    Event: class {constructor(type) { this.type = type; }},
    folderCache: new Map(), mailboxFolderKey: (folder, unread) => folder + unread,
    mailboxStatus: {},
    updateMailboxFolderCounts() {}, updateUnreadEmailAlert() {},
    globalMailboxClose: () => closeCount++,
    mailboxComposer: {dispatchEvent: () => closeCount++},
    [prefix + 'Form']: form, [prefix + 'Attachments']: [], [prefix + 'StoredAttachments']: [],
    [prefix + 'Submitting']: false, [prefix + 'DraftSavePromise']: null, [prefix + 'DraftSession']: 1,
    [prefix + 'DraftDirty']: false,
    [global ? 'globalMailboxSyncContent' : 'syncMailboxComposerContent']: () => {},
    [global ? 'globalMailboxClearDraftTimer' : 'clearMailboxComposerDraftTimer']: () => {},
    [global ? 'globalMailboxScheduleDraftSave' : 'scheduleMailboxComposerDraftSave']: () => { context[prefix + 'DraftDirty'] = true; },
    [global ? 'globalMailboxStatusMessage' : 'setMailboxComposerStatus']: (_text, isError) => { error = Boolean(isError); },
  };
  for (const name of ['AttachmentList', 'AttachmentInput', 'AttachmentSummary', 'DraftId', 'ScheduledEmailId', 'SaveDraft', 'Recipient', 'Cc', 'Subject', 'ContentValue', 'TemplateId']) {
    context[prefix + name] = element();
  }
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('app/static/email-draft-attachments.js', 'utf8'), context);
  vm.runInContext([setter, save, hasContent].map(name => extract(template, name)).join('\n'), context);
  return {context, prefix, requests, field, setter, save, hasContent, objectUrls, closed: () => closeCount, error: () => error};
}

async function exercise(global, scenario) {
  const f = fixture(global);
  const c = f.context;
  const p = f.prefix;
  const first = {name: 'offer.pdf'};
  const second = {name: 'new.pdf'};
  const attachment = {id: 'stored-offer', filename: first.name};
  c[f.setter]([first]);
  const localLink = c[p + 'AttachmentList'].children[0].children[0];
  assert.equal(localLink.tagName, 'a');
  assert.equal(localLink.download, first.name);
  assert.equal(f.objectUrls.get(localLink.href), first, 'Unsaved uploads download the original local file');
  assert.equal(c[f.hasContent](), true, 'Attachment-only drafts can be saved');
  const pending = c[f.save]({force: true, closeAfterSave: true});
  assert.deepEqual(Array.from(f.requests[0].options.body.fields.get('attachments')), [first]);
  assert.equal(f.requests[0].options.body.fields.get('retained_attachment_ids'), '[]');
  assert.equal(f.closed(), 0);
  if (scenario === 'edit-during-save') {
    c[p + 'AttachmentList'].children[0].children[1].events.click();
    c[f.setter]([second]);
  }
  if (scenario === 'new-session') {
    c[p + 'DraftSession']++;
    c[f.setter]([second]);
  }
  f.requests[0].resolve({ok: scenario !== 'failure', json: async () => scenario === 'failure'
    ? {detail: 'Storage full'} : {draft_id: 12, attachments: [attachment], uploaded_attachment_ids: ['stored-offer']}});
  await pending;
  if (scenario === 'failure') {
    assert.equal(f.closed(), 0);
    assert.equal(f.error(), true);
    assert.equal(c[p + 'AttachmentInput'].files[0], first, 'Failed saves keep the upload selection');
    return;
  }
  if (['edit-during-save', 'new-session'].includes(scenario)) {
    assert.equal(f.closed(), 0);
    assert.equal(c[p + 'StoredAttachments'].length, 0, 'Removed uploads must not return');
    assert.equal(c[p + 'AttachmentInput'].files[0], second, 'New files are not cleared');
    return;
  }
  assert.equal(f.closed(), 1);
  assert.equal(c[p + 'Attachments'].length, 0, 'Successfully stored files must not be uploaded again on send');
  assert.equal(c[p + 'AttachmentInput'].files.length, 0);
  assert.equal(c[p + 'StoredAttachments'][0].id, attachment.id);
  const storedLink = c[p + 'AttachmentList'].children[0].children[0];
  assert.equal(storedLink.tagName, 'a');
  assert.equal(storedLink.href, '/emails/unassigned/12/attachments/stored-offer');
  assert.equal(storedLink.download, first.name);
  assert.equal(f.objectUrls.size, 0, 'Persisted files release their previous browser object URLs');
  assert.equal(f.field.value, '["stored-offer"]');
  assert.ok(c[p + 'AttachmentList'].children[0].children[0].textContent.includes('gespeichert'));

  const again = c[f.save]({automatic: true});
  assert.equal(f.requests[1].options.body.fields.has('attachments'), false, 'Text edits do not upload the same file again');
  assert.equal(f.requests[1].options.body.fields.get('retained_attachment_ids'), '["stored-offer"]');
  f.requests[1].resolve({ok: true, json: async () => ({draft_id: 12, attachments: [attachment], uploaded_attachment_ids: []})});
  await again;
  assert.equal(f.closed(), 1, 'Autosave does not close');
  c[p + 'AttachmentList'].children[0].children[1].events.click();
  assert.equal(f.field.value, '[]', 'Send and save both see removal of stored files');
  assert.equal(c[p + 'StoredAttachments'].length, 0);
  assert.equal(c[p + 'DraftDirty'], true);
  assert.equal(c[f.hasContent](), true, 'Saving removal from an attachment-only draft must remain possible');
  const removed = c[f.save]({force: true});
  f.requests[2].resolve({ok: true, json: async () => ({draft_id: 12, attachments: [], uploaded_attachment_ids: []})});
  await removed;
  assert.equal(f.error(), false);
}

function checkScheduledAndSafeDownloadLinks() {
  const f = fixture(false);
  const c = f.context;
  c.mailboxComposerScheduledEmailId.value = '23';
  c.mailboxComposerStoredAttachments = [{id: 9, filename: 'scheduled.pdf'}];
  c[f.setter]([]);
  const link = c.mailboxComposerAttachmentList.children[0].children[0];
  assert.equal(link.href, '/emails/scheduled/23/attachments/9');
  assert.equal(link.download, 'scheduled.pdf');
  const helper = c.window.KosmosEmailDraftAttachments;
  const filename = '<img src=x onerror=alert(1)>.pdf';
  const escaped = helper.storedLink({id: '../other?x=1#hash', filename}, 12);
  assert.equal(escaped.textContent, filename + ' (gespeichert)');
  assert.equal(escaped.href, '/emails/unassigned/12/attachments/..%2Fother%3Fx%3D1%23hash');
  assert.equal(escaped.innerHTML, undefined, 'Filenames must remain plain text');
  assert.equal(helper.storedLink({id: 'file', filename}, '//evil.example').tagName, 'span');
  assert.equal(f.objectUrls.size, 0);
  c[f.setter]([{name: 'one.pdf'}]);
  assert.equal(f.objectUrls.size, 1);
  c[f.setter]([{name: 'two.pdf'}]);
  assert.equal(f.objectUrls.size, 1, 'Rerendering releases replaced local file URLs');
  c[f.setter]([]);
  assert.equal(f.objectUrls.size, 0);
}

async function sendWaitsForAttachmentUpload(global) {
  const f = fixture(global);
  const c = f.context;
  const prefix = f.prefix;
  c.syncMailboxComposerContent = () => {};
  c.updateMailboxComposerSubmit = () => {};
  c[prefix + 'Form'].reportValidity = () => true;
  c.mailboxComposerSubmit = element();
  c.globalMailboxSyncContent = () => {};
  c.globalMailboxUpdateSubmit = () => {};
  c.globalMailboxSubmit = element();
  c.window.location = {assign() {}};
  let sent = false;
  c.window.KosmosEmailDelivery = {submit: () => {
    sent = true;
    assert.equal(c[prefix + 'Attachments'].length, 0);
    assert.equal(f.field.value, '["saved"]');
    return Promise.resolve('/emails?folder=sent');
  }};
  const submit = global ? 'submitGlobalMailboxForm' : 'submitMailboxComposerForm';
  vm.runInContext(extract(global ? 'app/templates/base.html' : 'app/templates/emails.html', submit), c);
  c[f.setter]([{name: 'offer.pdf'}]);
  c[f.save]({automatic: true});
  const pending = c[submit]({preventDefault() {}});
  await new Promise(setImmediate);
  assert.equal(sent, false, 'Wait until an in-flight autosave reconciles uploaded files');
  f.requests[0].resolve({ok: true, json: async () => ({draft_id: 12, attachments: [{id: 'saved', filename: 'offer.pdf'}], uploaded_attachment_ids: ['saved']})});
  if (global) {
    await new Promise(setImmediate);
    assert.equal(f.requests[1].options.body.fields.has('attachments'), false, 'The before-send save does not duplicate autosaved uploads');
    f.requests[1].resolve({ok: true, json: async () => ({draft_id: 12, attachments: [{id: 'saved', filename: 'offer.pdf'}], uploaded_attachment_ids: []})});
  }
  await pending;
  assert.equal(sent, true);
}

(async () => {
  checkScheduledAndSafeDownloadLinks();
  for (const global of [false, true]) {
    for (const scenario of ['normal', 'failure', 'edit-during-save', 'new-session']) await exercise(global, scenario);
  }
  await sendWaitsForAttachmentUpload(false);
  await sendWaitsForAttachmentUpload(true);
})().catch(error => { console.error(error); process.exitCode = 1; });
