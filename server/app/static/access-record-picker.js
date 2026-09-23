(function () {
  'use strict';
  const layer = document.querySelector('[data-access-record-layer]');
  if (!layer) return;
  document.body.append(layer);
  const find = (name) => layer.querySelector('[data-access-record-' + name + ']');
  const drawer = layer.querySelector('[role="dialog"]');
  const search = find('search'), rows = find('rows'), all = find('all'), status = find('status');
  const applied = new WeakMap();
  const snapshots = new WeakMap();
  let snapshot = null, ready = false;
  let form = null, opener = null, selected = new Map(), items = [], page = 0, anchor = null;
  let complete = false, loading = false, generation = 0, controller = null, debounce = null, inert = [];
  const pageSize = 100;

  function selectedTeam(target) {
    const value = target.elements.team_id.value;
    return target.elements.kind.value === 'assign' && value && value !== '__keep__' ? value : '';
  }

  function updateCount() {
    const count = items.filter((item) => selected.has(item.record_id)).length;
    all.checked = items.length > 0 && count === items.length;
    all.indeterminate = count > 0 && count < items.length;
    all.disabled = loading || !complete || !items.length;
    find('count').textContent = selected.size + ' ausgewählt';
    find('apply').disabled = loading || !ready || selected.size > 5000;
    for (const row of rows.children) {
      const chosen = selected.has(row.dataset.id);
      row.classList.toggle('is-selected', chosen);
      row.querySelector('input').checked = chosen;
    }
  }

  function render() {
    rows.replaceChildren();
    for (const item of items.slice(page * pageSize, (page + 1) * pageSize)) {
      const row = document.createElement('tr');
      row.dataset.id = item.record_id;
      const cell = document.createElement('td');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.setAttribute('aria-label', item.name + ' auswählen');
      cell.append(checkbox);
      row.append(cell);
      for (const key of ['name', 'detail', 'status', 'owner', 'team']) {
        const td = document.createElement('td');
        td.textContent = item[key] || '-';
        row.append(td);
      }
      rows.append(row);
    }
    const pages = Math.max(1, Math.ceil(items.length / pageSize));
    find('page').textContent = 'Seite ' + (page + 1) + ' von ' + pages;
    find('prev').disabled = loading || page === 0;
    find('next').disabled = loading || page + 1 >= pages;
    updateCount();
  }

  async function load() {
    clearTimeout(debounce);
    if (!form) return;
    const requestId = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    loading = true;
    complete = false;
    ready = false;
    items = [];
    page = 0;
    anchor = null;
    status.classList.remove('notice-error');
    status.textContent = 'Datensätze werden geladen ...';
    render();
    const params = new URLSearchParams({module_key: form.elements.module_key.value, query: search.value.trim(), limit: '5000'});
    if (selectedTeam(form)) params.set('team_id', selectedTeam(form));
    try {
      const response = await fetch('/account/access/records/options?' + params, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: {Accept: 'application/json'}
      });
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Bitte erneut anmelden; die Auswahl bleibt erhalten.');
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Die Liste konnte nicht geladen werden.');
      if (requestId !== generation || !form) return;
      if (!Array.isArray(data.items) || !Number.isInteger(data.total)) throw new Error('Ungültige Listenantwort. Bitte erneut öffnen.');
      if (selectedTeam(form) && snapshot === null) {
        if (!Array.isArray(data.previous_record_ids) || !Array.isArray(data.selected_items)
          || data.previous_record_ids.length !== data.selected_items.length) {
          throw new Error('Die gespeicherten Teamzuweisungen konnten nicht vollständig geladen werden. Bitte erneut öffnen.');
        }
        snapshot = [...data.previous_record_ids];
        selected = new Map(data.selected_items.map((item) => [item.record_id, item]));
      }
      items = data.items;
      ready = true;
      complete = !data.next_offset && items.length === data.total;
      status.textContent = data.total + ' Treffer' + (complete ? '. „Alle Treffer“ umfasst alle Tabellenseiten.' : '. Bitte die Suche eingrenzen, um alle Treffer auswählen zu können.');
      if (!complete) status.classList.add('notice-error');
    } catch (error) {
      if (requestId !== generation || error.name === 'AbortError') return;
      status.textContent = error.message || 'Die Liste konnte nicht geladen werden.';
      status.classList.add('notice-error');
    } finally {
      if (requestId === generation && form) { loading = false; render(); }
    }
  }

  function close() {
    clearTimeout(debounce);
    generation += 1;
    if (controller) controller.abort();
    layer.hidden = true;
    layer.classList.remove('is-open');
    document.body.classList.remove('access-record-picker-open');
    inert.forEach(([node, wasInert]) => { node.inert = wasInert; });
    inert = [];
    form = null;
    opener?.focus();
  }

  function describeSelection(target) {
    const selection = applied.get(target) || new Map();
    target.elements.record_ids.value = JSON.stringify([...selection.keys()]);
    const names = [...selection.values()].map((item) => item.name);
    target.querySelector('[data-access-record-summary]').textContent = names.length
      ? names.length + ' ausgewählt: ' + names.slice(0, 3).join(', ') + (names.length > 3 ? ' und ' + (names.length - 3) + ' weitere.' : '.')
      : snapshots.has(target) ? '0 ausgewählt. Beim Speichern werden die geladenen Teamzuweisungen entfernt.' : 'Noch keine Datensätze ausgewählt.';
  }

  document.querySelectorAll('[data-access-record-form]').forEach((target) => {
    applied.set(target, new Map());
    target.querySelector('[data-access-record-open]').addEventListener('click', (event) => {
      form = target;
      opener = event.currentTarget;
      selected = new Map(applied.get(target));
      snapshot = snapshots.has(target) ? [...snapshots.get(target)] : null;
      document.getElementById('access-record-title').textContent = target.elements.module_key.selectedOptions[0].textContent + ' auswählen'
        + (selectedTeam(target) ? ' · ' + target.elements.team_id.selectedOptions[0].textContent : '');
      search.value = '';
      layer.hidden = false;
      layer.classList.add('is-open');
      document.body.classList.add('access-record-picker-open');
      inert = [...document.body.children].filter((node) => node !== layer && !['SCRIPT', 'STYLE', 'LINK'].includes(node.tagName))
        .map((node) => [node, node.inert]);
      inert.forEach(([node]) => { node.inert = true; });
      search.focus();
      load();
    });
    function resetSelection() {
      applied.set(target, new Map());
      snapshots.delete(target);
      if (target.elements.previous_record_ids) target.elements.previous_record_ids.disabled = true;
      describeSelection(target);
      if (selectedTeam(target)) target.querySelector('[data-access-record-summary]').textContent = '„Datensätze auswählen“ öffnen, um die gespeicherte Team-Auswahl zu bearbeiten.';
    }
    target.elements.module_key.addEventListener('change', resetSelection);
    target.elements.team_id.addEventListener('change', () => {
      if (target.elements.kind.value === 'assign') resetSelection();
    });
    target.addEventListener('submit', (event) => {
      const error = target.querySelector('[data-access-record-error]');
      error.hidden = true;
      let message = '';
      if (selectedTeam(target) && !snapshots.has(target)) message = 'Bitte zuerst die gespeicherte Team-Auswahl öffnen und übernehmen.';
      else if (!applied.get(target).size && !snapshots.has(target)) message = 'Bitte zuerst Datensätze auswählen.';
      else if (target.elements.kind.value === 'assign' && target.elements.team_id.value === '__keep__' && target.elements.owner_user_id.value === '__keep__') {
        message = 'Bitte ein Team oder einen Verantwortlichen zum Ändern wählen.';
      } else if (target.elements.kind.value === 'grant' && Boolean(target.elements.user_id.value) === Boolean(target.elements.team_id.value)) {
        message = 'Bitte genau einen Benutzer oder ein Team wählen.';
      }
      if (message) {
        event.preventDefault();
        error.textContent = message;
        error.hidden = false;
      }
      // The common form handler preserves all inputs on server errors.
    });
  });

  rows.addEventListener('click', (event) => {
    const row = event.target.closest('tr[data-id]');
    if (!row || loading) return;
    const item = items.find((entry) => entry.record_id === row.dataset.id);
    const checkbox = event.target.matches('input[type="checkbox"]');
    const start = items.findIndex((entry) => entry.record_id === anchor);
    const end = items.indexOf(item);
    const chosen = checkbox ? event.target.checked : !selected.has(item.record_id);
    // Row and checkbox clicks both toggle; never discard unrelated selections.
    if (event.shiftKey && start >= 0) {
      for (const entry of items.slice(Math.min(start, end), Math.max(start, end) + 1)) {
        if (!chosen) selected.delete(entry.record_id);
        else selected.set(entry.record_id, entry);
      }
    } else {
      if (chosen) selected.set(item.record_id, item);
      else selected.delete(item.record_id);
      anchor = item.record_id;
    }
    updateCount();
  });
  rows.addEventListener('mousedown', (event) => { if (event.shiftKey) event.preventDefault(); });
  all.addEventListener('change', () => {
    if (!complete || loading) return;
    for (const item of items) {
      if (all.checked) selected.set(item.record_id, item);
      else selected.delete(item.record_id);
    }
    updateCount();
  });
  find('clear').addEventListener('click', () => { selected.clear(); anchor = null; updateCount(); });
  find('prev').addEventListener('click', () => { page -= 1; render(); });
  find('next').addEventListener('click', () => { page += 1; render(); });
  find('apply').addEventListener('click', () => {
    if (!form || loading || !ready || selected.size > 5000) return;
    applied.set(form, new Map(selected));
    if (snapshot !== null) {
      snapshots.set(form, [...snapshot]);
      form.elements.previous_record_ids.value = JSON.stringify(snapshot);
      form.elements.previous_record_ids.disabled = false;
    }
    describeSelection(form);
    close();
  });
  layer.querySelectorAll('[data-access-record-close]').forEach((button) => button.addEventListener('click', close));
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    if (controller) controller.abort();
    generation += 1;
    loading = true;
    complete = false;
    items = [];
    render();
    debounce = setTimeout(load, 250);
  });
  layer.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === 'Enter' && event.target === search) { event.preventDefault(); load(); }
    if (event.key === 'Tab') {
      const focusable = [...drawer.querySelectorAll('button, input, select, [tabindex="0"]')].filter((node) => !node.disabled && node.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
})();
