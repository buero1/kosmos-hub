(function () {
  'use strict';
  var pending = new WeakSet();
  var notices = new WeakMap();
  var uncertain = 'Die Speicherung konnte nicht bestaetigt werden. Deine Eingaben bleiben erhalten. Bitte vor erneutem Speichern den gespeicherten Stand in einem zweiten Tab pruefen.';

  function showError(form, message) {
    var notice = notices.get(form);
    if (!notice || !notice.isConnected) {
      notice = document.createElement('p');
      notice.className = 'notice-error hub-form-error';
      notice.setAttribute('role', 'alert');
      notice.tabIndex = -1;
      // Some editors associate their controls with a hidden form (e.g. layouts).
      var container = form.getClientRects().length ? form : form.parentElement;
      while (container && !container.getClientRects().length) container = container.parentElement;
      (container || document.querySelector('.app-main-content') || document.body).prepend(notice);
      notices.set(form, notice);
    }
    notice.textContent = message;
    notice.hidden = false;
    notice.focus({preventScroll: true});
    notice.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }

  function jsonError(payload) {
    if (!payload) return '';
    var detail = payload.detail || payload.error;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map(function (entry) {
      return (entry.loc || []).filter(function (part) { return part !== 'body'; }).join(' / ') + ': ' + entry.msg;
    }).join('\n');
    return '';
  }

  async function submit(form, submitter, action) {
    pending.add(form);
    var buttons = Array.from(form.elements).filter(function (element) {
      return element.type === 'submit' || element.type === 'image';
    }).map(function (button) { return [button, button.disabled]; });
    var wasBusy = form.getAttribute('aria-busy');
    var previousNotice = notices.get(form);
    if (previousNotice) previousNotice.hidden = true;
    try {
      // Includes externally associated position fields, repeated values and files.
      // Run after editor submit handlers have synchronized their hidden inputs.
      var data = new FormData(form, submitter || undefined);
      buttons.forEach(function (entry) { entry[0].disabled = true; });
      form.setAttribute('aria-busy', 'true');
      var response = await window.fetch(action.href, {
        method: 'POST', body: data, credentials: 'same-origin', redirect: 'error',
        headers: {'X-Hub-Form': 'preserve', Accept: 'text/html, application/json'}
      });
      var contentType = response.headers.get('content-type') || '';
      var payload = null;
      var html = '';
      var message = '';
      if (contentType.includes('application/json')) {
        payload = await response.json();
        message = jsonError(payload);
      } else if (contentType.includes('text/html')) {
        html = await response.text();
        // Extract text only. Never replace the live editor with an error page.
        var page = new DOMParser().parseFromString(html, 'text/html');
        message = Array.from(page.querySelectorAll('.notice-error')).map(function (notice) {
          return notice.textContent.trim();
        }).filter(Boolean).join('\n');
      }
      if (!response.ok || message) {
        if (response.status === 401) message = 'Deine Sitzung ist abgelaufen. Bitte in einem zweiten Tab anmelden und hier erneut speichern. Deine Eingaben bleiben erhalten.';
        if (response.status === 403 && !message) message = 'Keine Berechtigung oder ungueltige Sitzung. Deine Eingaben bleiben erhalten.';
        throw new Error(message || (response.status >= 500 ? uncertain : 'Die Aktion konnte nicht abgeschlossen werden. Deine Eingaben bleiben erhalten.'));
      }
      if (payload && typeof payload.redirect_url === 'string' && payload.redirect_url) {
        var destination = new URL(payload.redirect_url, action);
        if (!['http:', 'https:'].includes(destination.protocol)) throw new Error(uncertain);
        var current = new URL(window.location.href);
        if (destination.origin === current.origin && destination.pathname === current.pathname && destination.search === current.search) {
          // Repeated saves often return the same success URL. Assigning only its
          // fragment keeps the old editor DOM, so explicitly fetch the saved page.
          window.history.replaceState(window.history.state, '', destination.href);
          window.location.reload();
        } else {
          window.location.assign(destination.href);
        }
      } else if (html) {
        // A few successful forms return a page directly (e.g. a newly issued token).
        window.history.replaceState(null, '', action.href);
        document.open();
        document.write(html);
        document.close();
      } else {
        throw new Error(uncertain);
      }
    } catch (error) {
      showError(form, error instanceof TypeError ? uncertain : (error.message || uncertain));
    } finally {
      buttons.forEach(function (entry) { entry[0].disabled = entry[1]; });
      if (wasBusy === null) form.removeAttribute('aria-busy');
      else form.setAttribute('aria-busy', wasBusy);
      pending.delete(form);
    }
  }

  // Window bubbling is after form/document handlers, including client validation,
  // confirmation dialogs and composers which already own their AJAX lifecycle.
  window.addEventListener('submit', function (event) {
    var form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    var button = event.submitter;
    var method = (button && button.getAttribute('formmethod')) || form.method;
    var target = (button && button.getAttribute('formtarget')) || form.target;
    if (method.toLowerCase() !== 'post' || (target && target !== '_self')) return;
    var action = new URL((button && button.getAttribute('formaction')) || form.action, window.location.href);
    if (action.origin !== window.location.origin) return;
    event.preventDefault();
    if (!pending.has(form)) return submit(form, button, action);
  });
})();
