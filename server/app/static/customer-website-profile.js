(() => {
  const layer = document.querySelector('[data-website-profile-layer]');
  if (!layer) return;
  const find = name => layer.querySelector(`[data-website-profile-${name}]`);
  const form = find('form'), target = find('target'), rows = find('rows'), send = find('send');
  const status = find('status'), error = find('error'), all = find('all');
  const base = `/customers/${layer.dataset.customerId}/website-profile`;
  let preview = null, generation = 0, busy = false, opener = null;
  const boxes = () => Array.from(rows.querySelectorAll('input[type=checkbox]:not(:disabled)'));
  function selection() {
    const inputs = boxes(), checked = inputs.filter(box => box.checked).length;
    send.disabled = busy || !preview || !checked;
    all.checked = !!checked && checked === inputs.length;
    all.indeterminate = checked > 0 && checked < inputs.length;
    all.disabled = busy || !inputs.length;
  }
  function message(value) { error.textContent = value; error.hidden = !value; }
  async function json(response) {
    const data = await response.json();
    if (!response.ok) {
      const failure = new Error(typeof data.detail === 'string' ? data.detail : 'Die Anfrage wurde abgewiesen.');
      failure.status = response.status; throw failure;
    }
    return data;
  }
  async function load(site = '') {
    const ticket = ++generation;
    preview = null; busy = true; selection(); message(''); rows.replaceChildren();
    target.disabled = true; find('reload').disabled = true; status.textContent = 'Firmenprofil wird gelesen ...';
    try {
      const data = await fetch(`${base}/preview${site ? '?site_id=' + encodeURIComponent(site) : ''}`, {cache: 'no-store'}).then(json);
      if (ticket !== generation || layer.hidden) return;
      preview = data; target.replaceChildren();
      for (const option of data.options) {
        const el = document.createElement('option'); el.value = option.site_id;
        el.textContent = `${option.domain} (${option.source})`; target.append(el);
      }
      target.value = data.site_id;
      find('source').textContent = `Ziel aus: ${data.target_source}. Keine automatische Ausweich-Website bei Verbindungsfehlern.`;
      for (const row of data.rows) {
        const tr = document.createElement('tr'), cell = document.createElement('td'), checkbox = document.createElement('input');
        checkbox.type = 'checkbox'; checkbox.value = row.id;
        checkbox.disabled = !row.editable || !row.proposed.trim(); checkbox.checked = row.selectable;
        checkbox.setAttribute('aria-label', row.label + ' übertragen'); cell.append(checkbox); tr.append(cell);
        for (const value of [row.label, String(row.current || '-')]) {
          const td = document.createElement('td'); td.textContent = value; tr.append(td);
        }
        const valueCell = document.createElement('td'); tr.append(valueCell);
        if (row.editable) {
          const input = document.createElement(row.type === 'textarea' ? 'textarea' : 'input');
          if (row.type !== 'textarea') input.type = ({email: 'email', url: 'url', date: 'date', datetime: 'datetime-local'})[row.type] || 'text';
          input.value = row.proposed; input.maxLength = row.max_length;
          input.className = 'website-profile-value'; input.dataset.fieldId = row.id;
          input.setAttribute('aria-label', row.label + ': Neuer Wert');
          input.placeholder = 'Optional ergänzen';
          input.addEventListener('input', () => {
            checkbox.disabled = !input.value.trim(); checkbox.checked = !checkbox.disabled; selection();
          });
          valueCell.append(input);
        } else valueCell.textContent = row.proposed || '-';
        const hint = document.createElement('small'); hint.className = 'subtle'; hint.textContent = row.reason || row.source;
        tr.lastElementChild.append(hint); rows.append(tr);
      }
      status.textContent = 'Vorschau geladen. Erst die Bestätigung überträgt die ausgewählten Werte.';
    } catch (e) {
      if (ticket === generation && !layer.hidden) { status.textContent = ''; message(e.message || 'Vorschau konnte nicht geladen werden.'); }
    } finally {
      if (ticket === generation) { busy = false; target.disabled = !preview || preview.options.length < 2; find('reload').disabled = false; selection(); }
    }
  }
  function close() {
    if (busy && preview) return;
    generation++; layer.hidden = true; layer.classList.remove('is-open'); document.body.classList.remove('website-profile-open');
    rows.replaceChildren(); preview = null; opener?.focus();
  }
  document.querySelectorAll('[data-website-profile-open]').forEach(button => button.addEventListener('click', () => {
    opener = button; button.closest('details')?.removeAttribute('open'); layer.hidden = false; layer.classList.add('is-open');
    document.body.classList.add('website-profile-open'); layer.querySelector('[role=dialog]').focus(); load();
  }));
  layer.querySelectorAll('[data-website-profile-close]').forEach(button => button.addEventListener('click', close));
  target.addEventListener('change', () => load(target.value));
  find('reload').addEventListener('click', () => load(target.value));
  rows.addEventListener('change', selection);
  all.addEventListener('change', () => { boxes().forEach(box => { box.checked = all.checked; }); selection(); });
  layer.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(layer.querySelectorAll('aside button, aside input, aside textarea, aside select, aside a[href]')).filter(el => !el.disabled && el.offsetParent !== null);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && (document.activeElement === first || document.activeElement.matches('[role=dialog]'))) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !preview || send.disabled) return;
    const selected = boxes().filter(box => box.checked), edited = Object.create(null);
    for (const box of selected) {
      const input = box.closest('tr').querySelector('.website-profile-value');
      if (!input.reportValidity()) return;
      edited[box.value] = input.value;
    }
    busy = true; selection(); message(''); target.disabled = true; find('reload').disabled = true;
    status.textContent = 'Bestätigte Übertragung wird beauftragt ...';
    const body = new URLSearchParams({csrf_token: form.elements.csrf_token.value, confirmed: 'yes', site_id: preview.site_id, preview_token: preview.preview_token});
    selected.forEach(box => body.append('field_ids', box.value));
    body.set('edited_values_json', JSON.stringify(edited));
    const controls = Array.from(rows.querySelectorAll('input, textarea')).map(el => [el, el.disabled]);
    controls.forEach(([el]) => { el.disabled = true; });
    try {
      const data = await fetch(`${base}/send`, {method: 'POST', body}).then(json);
      preview = null;
      status.replaceChildren(document.createTextNode('Übertragung beauftragt. '));
      const link = document.createElement('a'); link.href = `/wordpress/jobs/${Number(data.job_id)}`; link.textContent = 'Ergebnis und Protokoll ansehen'; status.append(link);
    } catch (e) {
      message(e.message || 'Ergebnis unklar. Zuerst das Firmenprofil und die WordPress-Aufträge prüfen; nicht ungeprüft erneut senden.');
      // Only a known validation rejection is safe to correct and resubmit. A transport error may follow a committed job.
      if (e.status !== 422) preview = null;
      status.textContent = e.status === 422 ? 'Nicht gesendet. Bitte Eingaben korrigieren; alle Änderungen bleiben erhalten.' : 'Nicht erneut senden, bevor Vorschau bzw. Auftragsstatus geprüft wurde.';
    } finally {
      controls.forEach(([el, disabled]) => { el.disabled = disabled; });
      busy = false; target.disabled = !preview || preview.options.length < 2; find('reload').disabled = false; selection();
    }
  });
})();
