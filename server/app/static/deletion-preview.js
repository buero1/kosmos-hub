(function () {
  'use strict';
  var states = new WeakMap();
  var supported = /^\/(?:leads\/\d+|finance\/(?:articles|offers|orders|invoices|dunnings|recurring-invoices)\/\d+|contacts\/\d+|cases\/\d+|(?:customers|leads)\/\d+\/(?:activities\/(?:calls|tasks|meetings)|(?:communications\/)?notes)\/\d+)\/delete$/;

  function pathFor(form) {
    var url = new URL(form.action, location.href);
    return url.origin === location.origin && supported.test(url.pathname) ? url.pathname : '';
  }

  function paragraph(parent, text, className) {
    var element = document.createElement('p');
    element.textContent = text;
    if (className) element.className = className;
    parent.appendChild(element);
    return element;
  }

  function list(parent, title, entries) {
    if (!entries.length) return;
    paragraph(parent, title).style.fontWeight = '600';
    var ul = document.createElement('ul');
    entries.forEach(function (text) { var li = document.createElement('li'); li.textContent = text; ul.appendChild(li); });
    parent.appendChild(ul);
  }

  async function refresh(form, host) {
    var path = pathFor(form);
    if (!path) return;
    host = host || form;
    host.closest('dialog').classList.add('deletion-impact-dialog');
    var previous = states.get(form);
    if (previous) { previous.controller.abort(); previous.panel.remove(); }
    var state = {path: path, ready: false, host: host, controller: new AbortController(), panel: document.createElement('section')};
    states.set(form, state);
    state.panel.setAttribute('data-deletion-impact', '');
    state.panel.setAttribute('aria-live', 'polite');
    var actions = host.querySelector('.dialog-actions');
    host.insertBefore(state.panel, actions || null);
    var buttons = Array.from(host.querySelectorAll('button[type="submit"], input[type="submit"], [data-deletion-confirm]'));
    buttons.forEach(function (button) { button.disabled = true; });
    paragraph(state.panel, 'L\u00f6schfolgen werden gepr\u00fcft ...');
    var timeout = window.setTimeout(function () { state.controller.abort(); }, 15000);
    try {
      var response = await fetch('/deletion-preview?target_path=' + encodeURIComponent(path), {
        credentials: 'same-origin', signal: state.controller.signal, headers: {Accept: 'application/json'}
      });
      var data = await response.json();
      if (states.get(form) !== state) return;
      if (!response.ok || !Array.isArray(data.deleted) || !Array.isArray(data.retained) || !Array.isArray(data.blockers)) {
        throw new Error(data.detail || 'Die L\u00f6schfolgen konnten nicht gepr\u00fcft werden.');
      }
      state.panel.replaceChildren();
      list(state.panel, 'Wird gel\u00f6scht:', data.deleted);
      list(state.panel, 'Bleibt erhalten:', data.retained);
      data.blockers.forEach(function (message) { paragraph(state.panel, message, 'notice-error'); });
      if (data.blockers.length) {
        var link = document.createElement('a');
        link.href = '/emails?folder=planned';
        link.textContent = 'Geplante E-Mails \u00f6ffnen';
        state.panel.appendChild(link);
      } else {
        paragraph(state.panel, 'Endg\u00fcltiges L\u00f6schen: Diese Daten landen noch nicht in einem Hub-Papierkorb.', 'subtle');
        state.ready = true;
        buttons.forEach(function (button) { button.disabled = false; });
      }
    } catch (error) {
      if (states.get(form) !== state) return;
      state.panel.replaceChildren();
      paragraph(state.panel, error.name === 'AbortError' ? 'Die Pr\u00fcfung dauert zu lange. Bitte erneut versuchen.' : error.message, 'notice-error');
      var retry = document.createElement('button');
      retry.type = 'button'; retry.className = 'button button-secondary'; retry.textContent = 'Erneut pr\u00fcfen';
      retry.addEventListener('click', function () { refresh(form, host); });
      state.panel.appendChild(retry);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function confirmStandalone(form, submitter) {
    var dialog = document.createElement('dialog');
    dialog.className = 'update-confirmation-dialog';
    dialog.setAttribute('aria-label', 'Endg\u00fcltig l\u00f6schen?');
    var host = document.createElement('div');
    paragraph(host, 'Endg\u00fcltig l\u00f6schen?').style.fontWeight = '600';
    var actions = document.createElement('div');
    actions.className = 'dialog-actions';
    var cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'button button-secondary'; cancel.textContent = 'Abbrechen';
    cancel.addEventListener('click', function () { dialog.close(); });
    var confirm = document.createElement('button');
    confirm.type = 'button'; confirm.className = 'button button-danger'; confirm.textContent = 'L\u00f6schen';
    confirm.setAttribute('data-deletion-confirm', '');
    confirm.addEventListener('click', function () {
      var state = states.get(form);
      if (!state || !state.ready || state.path !== pathFor(form)) return;
      state.approved = true;
      form.requestSubmit(submitter || undefined);
      dialog.close();
    });
    actions.append(cancel, confirm); host.appendChild(actions); dialog.appendChild(host); document.body.appendChild(dialog);
    dialog.addEventListener('close', function () { states.delete(form); dialog.remove(); });
    dialog.showModal(); refresh(form, host);
  }

  function inspect(dialog) {
    if (!dialog.open) return;
    dialog.querySelectorAll('form').forEach(function (form) { if (pathFor(form)) refresh(form); });
  }
  new MutationObserver(function (mutations) {
    var dialogs = new Set();
    mutations.forEach(function (mutation) {
      var dialog = mutation.target.closest('dialog');
      if (dialog) dialogs.add(dialog);
    });
    dialogs.forEach(inspect);
  }).observe(document.documentElement, {subtree: true, attributes: true, attributeFilter: ['open', 'action']});

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !pathFor(form)) return;
    var state = states.get(form);
    if (state && state.approved && state.path === pathFor(form)) { state.approved = false; return; }
    if (state && state.ready && state.path === pathFor(form) && form.closest('dialog[open]')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    var dialog = form.closest('dialog');
    if (dialog) { if (!dialog.open) dialog.showModal(); else refresh(form); }
    else confirmStandalone(form, event.submitter);
  }, true);
  document.querySelectorAll('dialog[open]').forEach(inspect);
})();
