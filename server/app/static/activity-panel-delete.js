(function () {
  'use strict';
  var dialog = document.querySelector('[data-activity-panel-delete-dialog]');
  if (!dialog) return;
  var confirm = dialog.querySelector('[data-activity-panel-delete-confirm]');
  var cancel = dialog.querySelector('[data-activity-panel-delete-cancel]');
  var errorNotice = dialog.querySelector('[data-activity-panel-delete-error]');
  var pending = null;
  var busy = false;
  var labels = {call:'Anruf', task:'Aufgabe', meeting:'Meeting'};
  var uncertain = 'Die Loeschung konnte nicht bestaetigt werden. Deine Eingaben bleiben erhalten. Bitte den Stand vor einem erneuten Versuch in einem zweiten Tab pruefen.';

  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-activity-delete-open]');
    if (!button || button.hidden || button.disabled || busy) return;
    var kind = button.dataset.activityDeleteKind;
    var url = button.dataset.activityDeleteUrl || '';
    if (!labels[kind] || !new RegExp('^/activities/' + kind + '/[1-9]\\d*/delete$').test(url)) return;
    event.preventDefault();
    var form = button.closest('form');
    var csrf = form && form.elements.namedItem('csrf_token');
    if (!csrf) return;
    pending = {url:url, csrf:csrf.value, kind:kind};
    dialog.querySelector('[data-activity-panel-delete-heading]').textContent = labels[kind] + ' l\u00f6schen?';
    dialog.querySelector('[data-activity-panel-delete-name]').textContent = button.dataset.activityDeleteName || labels[kind];
    errorNotice.hidden = true;
    dialog.showModal();
    cancel.focus();
  });
  cancel.addEventListener('click', function () { if (!busy) dialog.close(); });
  dialog.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') event.stopPropagation();
  });
  dialog.addEventListener('cancel', function (event) { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', function () { pending = null; });
  confirm.addEventListener('click', async function () {
    if (!pending || busy) return;
    busy = true;
    confirm.disabled = cancel.disabled = true;
    dialog.setAttribute('aria-busy', 'true');
    errorNotice.hidden = true;
    try {
      var data = new FormData();
      data.append('csrf_token', pending.csrf);
      var response = await fetch(pending.url, {method:'POST', body:data, credentials:'same-origin', redirect:'error', headers:{Accept:'application/json'}});
      var payload = (response.headers.get('content-type') || '').includes('application/json') ? await response.json() : null;
      if (!response.ok || !payload || payload.ok !== true) {
        var message = payload && typeof payload.detail === 'string' ? payload.detail : uncertain;
        if (response.status === 401) message = 'Deine Sitzung ist abgelaufen. Bitte in einem zweiten Tab anmelden. Deine Eingaben bleiben erhalten.';
        throw new Error(message);
      }
      // Reload the current view, retaining filters/week/anchor, only after confirmed deletion.
      var destination = new URL(window.location.href);
      destination.searchParams.delete('create');
      var state = destination.pathname === '/calendar' ? 'calendar' : 'activity';
      destination.searchParams.set(state, 'success');
      destination.searchParams.set(state + '_message', payload.message || 'Aktivit\u00e4t wurde gel\u00f6scht.');
      window.history.replaceState(window.history.state, '', destination.href);
      window.location.reload();
    } catch (error) {
      errorNotice.textContent = error instanceof TypeError ? uncertain : (error.message || uncertain);
      errorNotice.hidden = false;
    } finally {
      busy = false;
      confirm.disabled = cancel.disabled = false;
      dialog.removeAttribute('aria-busy');
    }
  });
})();
