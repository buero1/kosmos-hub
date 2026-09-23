(() => {
  const form = document.querySelector('[data-plugin-auto-updates]');
  const dialog = document.querySelector('[data-auto-update-confirmation]');
  if (!form || !dialog) return;
  const sites = () => [...document.querySelectorAll('[data-site-selector] input[name="site_id"]:checked')];
  const plugins = () => [...form.querySelectorAll('input[name="plugin_file"]:checked')];
  let approved = false;
  function sync() {
    approved = false;
    form.elements.confirmed.value = '';
    form.querySelector('[data-auto-update-summary]').textContent = `${sites().length} Websites und ${plugins().length} Plugins ausgewaehlt.`;
    document.dispatchEvent(new Event('plugin-auto-policy-change'));
  }
  form.addEventListener('change', sync);
  document.addEventListener('site-selector-change', sync);
  form.addEventListener('submit', event => {
    if (approved) { approved = false; return; }
    event.preventDefault();
    if (!sites().length || !plugins().length) return;
    const blocked = form.elements.blocked.value === 'true';
    dialog.querySelector('[data-auto-update-confirmation-text]').textContent = blocked
      ? `Automatische Updates fuer ${plugins().length} Plugins auf ${sites().length} Websites sperren?`
      : `Hub-Sperre fuer ${plugins().length} Plugins auf ${sites().length} Websites aufheben? Die Automatik wird dadurch nicht eingeschaltet.`;
    const list = dialog.querySelector('[data-auto-update-confirmation-list]');
    list.replaceChildren();
    const names = plugins().map(input => input.dataset.pluginLabel || input.value);
    for (const name of [...names, ...sites().map(input => input.closest('label').querySelector('span').textContent.trim())]) {
      const item = document.createElement('li');
      item.textContent = name;
      list.append(item);
    }
    dialog.showModal();
  });
  dialog.querySelector('[data-auto-update-cancel]').addEventListener('click', () => dialog.close());
  dialog.querySelector('[data-auto-update-confirm]').addEventListener('click', () => {
    const container = form.querySelector('[data-auto-update-sites]');
    container.replaceChildren();
    for (const site of sites()) {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = 'site_id'; input.value = site.value;
      container.append(input);
    }
    form.elements.confirmed.value = 'yes';
    approved = true;
    dialog.close();
    form.requestSubmit();
  });
  sync();
})();
