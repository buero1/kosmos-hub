const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function extract(path, name) {
  const source = fs.readFileSync(path, 'utf8');
  const match = source.match(new RegExp('^([ \\t]*)function ' + name + '\\([^\\n]*\\) \\{[\\s\\S]*?^\\1\\}', 'm'));
  assert.ok(match, name);
  return match[0];
}

async function exercise(globalComposer, failure, currentPath = '/customers/411', targetPath = '/emails?folder=sent&selected=unassigned-52') {
  const prefix = globalComposer ? 'globalMailbox' : 'mailboxComposer';
  const handler = globalComposer ? 'submitGlobalMailboxForm' : 'submitMailboxComposerForm';
  const path = globalComposer ? 'app/templates/base.html' : 'app/templates/emails.html';
  const update = globalComposer ? 'globalMailboxUpdateSubmit' : 'updateMailboxComposerSubmit';
  const fields = {
    recipient_email: 'recipient@example.test', sender_email: 'sender@example.test', cc_emails: 'cc@example.test',
    subject: 'Unchanged subject', content: '<p>Unchanged HTML</p>', template_id: '7', lead_id: '42', draft_id: '12',
    attachments: [{name: 'offer.pdf', bytes: '%PDF-example'}], scheduled_at: '',
  };
  const original = JSON.stringify(fields);
  const form = {fields, action: '/emails/send', reportValidity: () => true};
  let resolveFetch;
  let rejectFetch;
  const navigations = [];
  const replacements = [];
  let reloads = 0;
  const requests = [];
  const statuses = [];
  const status = {dataset: {}, set textContent(message) { statuses.push(message); }};
  const setStatus = globalComposer ? 'globalMailboxStatusMessage' : 'setMailboxComposerStatus';
  const context = {
    URL,
    scopedMailboxUrl: value => new URL(value, 'https://hub.example.test'),
    window: {
      location: {
        href: new URL(currentPath, 'https://hub.example.test').href,
        assign: url => navigations.push(url),
        reload: () => { reloads++; },
      },
      history: {state: {retained: true}, replaceState: (state, title, url) => replacements.push({state, url})},
      fetch: (url, options) => {
        requests.push({url, options});
        return new Promise((resolve, reject) => { resolveFetch = resolve; rejectFetch = reject; });
      },
    },
    FormData: class { constructor(form) { this.fields = {...form.fields}; } },
  };
  Object.assign(context, {
    [prefix + 'Form']: form, [prefix + 'Submitting']: false,
    [prefix + 'DraftSavePromise']: null,
    [prefix + 'Submit']: {disabled: false}, [prefix + 'SaveDraft']: {disabled: false},
    [prefix + 'Sender']: {value: fields.sender_email, selectedOptions: [{dataset: {canSend: 'true'}}]}, [prefix + 'Recipient']: {validity: {valid: true}},
    [prefix + 'ScheduledAt']: {value: ''},
    [globalComposer ? 'globalMailboxHasDraftContent' : 'mailboxComposerHasDraftContent']: () => true,
    [globalComposer ? 'globalMailboxSyncContent' : 'syncMailboxComposerContent']: () => {},
    [globalComposer ? 'globalMailboxClearDraftTimer' : 'clearMailboxComposerDraftTimer']: () => {},
    [prefix + 'Status']: status,
    saveGlobalMailboxDraft: () => Promise.resolve(true),
  });
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('app/static/email-delivery.js', 'utf8'), context);
  vm.runInContext(extract(path, handler) + '\n' + extract(path, update) + '\n' + extract(path, setStatus), context);
  const event = () => ({defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }});
  context[handler]({defaultPrevented: true});
  assert.equal(requests.length, 0, 'A pending AI suggestion must still block delivery');
  const pending = context[handler](event());
  await new Promise(setImmediate);
  context[handler](event());
  context[update]();
  assert.equal(requests.length, 1, 'No duplicate submits');
  assert.equal(context[prefix + 'Submit'].disabled, true);
  assert.equal(requests[0].options.headers.Accept, 'application/json');
  assert.equal(requests[0].options.body.fields.attachments, fields.attachments);
  if (failure === 'network') rejectFetch(new TypeError('Failed to fetch'));
  else if (failure === 'html') resolveFetch({ok: true, json: async () => { throw new Error('login page'); }});
  else resolveFetch({ok: false, json: async () => ({detail: 'Recipient refused'})});
  await pending;
  assert.deepEqual(navigations, [], 'The selected folder and page must stay unchanged');
  assert.equal(reloads, 0, 'Do not reload on uncertain or failed delivery');
  assert.deepEqual(replacements, []);
  assert.equal(JSON.stringify(fields), original, 'Do not clear text, context, or attachments');
  assert.equal(context[prefix + 'Submitting'], false);
  assert.equal(context[prefix + 'Submit'].disabled, false);
  assert.equal(context[prefix + 'SaveDraft'].disabled, false);
  assert.ok(statuses.at(-1).includes(failure === 'smtp' ? 'Recipient refused' : 'Versandstatus'));
  assert.equal(status.dataset.status, 'error', 'Failure messages must use the red error styling');
  assert.equal(requests.length, 1, 'No automatic retry after uncertain delivery');
  const retry = context[handler](event());
  await new Promise(setImmediate);
  assert.equal(status.dataset.status, 'info', 'A new send attempt resets the error styling');
  resolveFetch({ok: true, json: async () => ({redirect_url: targetPath})});
  await retry;
  const current = new URL(context.window.location.href);
  const target = new URL(targetPath, current);
  if (current.pathname === target.pathname && current.search === target.search) {
    assert.equal(reloads, 1, 'Same-page success must reload, not leave the composer submitting');
    assert.deepEqual(navigations, []);
    assert.deepEqual(replacements, [{state: {retained: true}, url: target.href}], 'Keep success anchor and history state');
  } else {
    assert.equal(reloads, 0);
    assert.deepEqual(navigations, [target.href]);
    assert.deepEqual(replacements, []);
  }
  assert.equal(requests.length, 2, 'Navigation must not resubmit the email');
}

(async () => {
  for (const globalComposer of [false, true]) {
    for (const failure of ['smtp', 'network', 'html']) await exercise(globalComposer, failure);
    const target = '/customers/411?communication=success&message=sent#customer-communications';
    for (const current of [target, target.split('#')[0], target.replace('#customer-communications', '#customer-fields')]) {
      await exercise(globalComposer, 'smtp', current, target);
    }
    await exercise(globalComposer, 'smtp', '/customers/411?communication=error', target);
    await exercise(globalComposer, 'smtp', '/customers/412?communication=success&message=sent', target);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
