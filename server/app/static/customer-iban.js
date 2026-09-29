(function () {
  'use strict';
  var resets = [];
  document.querySelectorAll('[data-iban-reveal]').forEach(function (widget) {
    var show = widget.querySelector('[data-iban-show]');
    var label = widget.querySelector('[data-iban-show-label]');
    var value = widget.querySelector('[data-iban-value]');
    var copy = widget.querySelector('[data-iban-copy]');
    var status = widget.querySelector('[data-iban-status]');
    var timer, controller, generation = 0;
    function hide() {
      generation++;
      window.clearTimeout(timer);
      if (controller) controller.abort();
      controller = null;
      value.textContent = '';
      value.hidden = true;
      copy.hidden = true;
      copy.disabled = false;
      show.disabled = false;
      show.setAttribute('aria-expanded', 'false');
      show.setAttribute('aria-label', 'IBAN anzeigen');
      label.textContent = 'Anzeigen';
      status.textContent = '';
    }
    resets.push(hide);
    show.addEventListener('click', async function () {
      if (!value.hidden) { hide(); return; }
      resets.forEach(function (reset) { reset(); });
      var attempt = generation;
      controller = new AbortController();
      timer = window.setTimeout(hide, 30000);
      show.disabled = true;
      status.textContent = 'IBAN wird geladen ...';
      try {
        var response = await window.fetch(widget.dataset.ibanUrl, {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
          headers: {'Accept': 'application/json'},
          body: new URLSearchParams({csrf_token: widget.dataset.ibanCsrf}), signal: controller.signal
        });
        var payload = await response.json();
        if (attempt !== generation || document.hidden) return;
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Die IBAN konnte nicht geladen werden.');
        if (typeof payload.iban !== 'string' || !payload.iban || payload.iban.length > 64) throw new Error('Die IBAN konnte nicht geladen werden.');
        value.textContent = payload.iban;
        value.hidden = false;
        copy.hidden = false;
        label.textContent = 'Ausblenden';
        show.setAttribute('aria-expanded', 'true');
        show.setAttribute('aria-label', 'IBAN ausblenden');
        status.textContent = 'Wird nach 30 Sekunden automatisch ausgeblendet.';
        window.clearTimeout(timer);
        timer = window.setTimeout(hide, 30000);
      } catch (error) {
        if (attempt !== generation) return;
        hide();
        status.textContent = error.name === 'AbortError' ? '' : (error.message || 'Die IBAN konnte nicht geladen werden.');
      } finally {
        if (attempt === generation) show.disabled = false;
      }
    });
    copy.addEventListener('click', async function () {
      if (value.hidden || !value.textContent) return;
      var attempt = generation;
      copy.disabled = true;
      try {
        await navigator.clipboard.writeText(value.textContent);
        if (attempt === generation) status.textContent = 'IBAN kopiert. Die Zwischenablage bleibt erhalten.';
      } catch (_) {
        if (attempt === generation) status.textContent = 'Kopieren nicht erlaubt. Du kannst die angezeigte IBAN markieren.';
      } finally {
        if (attempt === generation) copy.disabled = false;
      }
    });
    document.addEventListener('click', function (event) { if (!widget.contains(event.target)) hide(); });
  });
  function hideAll() { resets.forEach(function (reset) { reset(); }); }
  document.addEventListener('visibilitychange', function () { if (document.hidden) hideAll(); });
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape') hideAll(); });
  window.addEventListener('pagehide', hideAll);
})();
