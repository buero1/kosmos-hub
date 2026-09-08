import { Jodit } from 'jodit';
import 'jodit/esm/plugins/all.js';
import de from 'jodit/esm/langs/de.js';
import 'jodit/es2021/jodit.min.css';

Jodit.lang.de = de;
Jodit.modules.Icon.set('email-button', `
  <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <rect x="1.5" y="3" width="13" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>
    <path d="M5 8h6M9 5l3 3-3 3" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"/>
  </svg>
`);

const editors = new WeakMap();
const toolbar = [
  'undo', 'redo', '|',
  'bold', 'italic', 'underline', 'strikethrough', '|',
  'ul', 'ol', '|',
  'font', 'fontsize', 'brush', '|',
  'align', '|',
  'link', 'emailButton', 'localImage', 'table', '|',
  'eraser', 'source',
];

const localImagePath = /^\/emails\/compose\/images\/[A-Za-z0-9_-]{32,64}$/;

function editorDefaults() {
  const fontFamily = document.body.dataset.emailComposerFontFamily || 'Verdana, Geneva, sans-serif';
  const fontSize = Number.parseInt(document.body.dataset.emailComposerFontSize || '12', 10);
  const lineHeight = Number.parseFloat(document.body.dataset.emailComposerLineHeight || '1.1');
  return {
    fontFamily,
    fontSize: Number.isFinite(fontSize) ? fontSize : 12,
    lineHeight: Number.isFinite(lineHeight) ? lineHeight : 1.1,
  };
}

function emailDocumentStyle(defaults) {
  // Match the standalone document used for sent-mail previews, not the Hub page.
  return `html { background: #fff; }\nbody { color: #000; font-family: ${defaults.fontFamily}; font-size: ${defaults.fontSize}px; line-height: ${defaults.lineHeight}; }\np { margin: 0 0 10px; line-height: ${defaults.lineHeight}; }\np:last-child { margin-bottom: 0; }`;
}

function csrfTokenFor(host) {
  return host.closest('form')?.querySelector('input[name="csrf_token"]')?.value || '';
}

function imageError(editor, error) {
  const message = error instanceof Error ? error.message : 'Das Bild konnte nicht eingefügt werden.';
  editor.message.error(message);
}

function chooseLocalImage(editor, host) {
  const picker = document.createElement('input');
  picker.type = 'file';
  picker.accept = 'image/png,image/jpeg,image/gif,image/webp';
  picker.addEventListener('change', async () => {
    const file = picker.files?.[0];
    picker.remove();
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      imageError(editor, new Error('Ein eingefügtes Bild darf höchstens 5 MB groß sein.'));
      return;
    }

    const csrfToken = csrfTokenFor(host);
    if (!csrfToken) {
      imageError(editor, new Error('Die Bilddatei konnte nicht sicher hochgeladen werden.'));
      return;
    }

    const formData = new FormData();
    formData.append('image', file, file.name);
    formData.append('csrf_token', csrfToken);
    try {
      const response = await window.fetch('/emails/compose/images', {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.success !== true || typeof payload.url !== 'string' || !localImagePath.test(payload.url)) {
        throw new Error(payload.detail || 'Das Bild konnte nicht im Hub gespeichert werden.');
      }

      editor.s.restore();
      const image = editor.createInside.element('img');
      image.src = payload.url;
      image.alt = file.name;
      await editor.s.insertImage(image, null, editor.o.imageDefaultWidth);
      editor.e.fire('change');
    } catch (error) {
      imageError(editor, error);
    }
  }, { once: true });
  editor.s.save();
  picker.click();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[character]));
}

function normalizeButtonUrl(value) {
  const candidate = value.trim();
  if (!candidate) return null;

  const withProtocol = /^(?:https?:|mailto:|tel:)/i.test(candidate)
    ? candidate
    : `https://${candidate}`;
  try {
    const parsed = new URL(withProtocol);
    if (!['http:', 'https:', 'mailto:', 'tel:'].includes(parsed.protocol)) return null;
    if (['http:', 'https:'].includes(parsed.protocol) && !parsed.hostname) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

function emailButtonMarkup(label, url) {
  return `
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width: 100%; border-collapse: collapse;">
  <tr>
    <td align="center" style="padding: 12px 0;">
      <a class="hub-email-button" href="${escapeHtml(url)}" style="display: inline-block; background-color: #0e7c66; border-radius: 6px; color: #ffffff; font-family: Arial, Helvetica, sans-serif; font-size: 14px; font-weight: 700; line-height: 1; padding: 13px 20px; text-align: center; text-decoration: none;">${escapeHtml(label)}</a>
    </td>
  </tr>
</table>`;
}

function openButtonDialog(editor) {
  editor.s.save();

  const dialog = document.createElement('dialog');
  dialog.className = 'email-editor-button-dialog';
  dialog.innerHTML = `
    <form method="dialog" novalidate>
      <h3>Button einfügen</h3>
      <p class="subtle">Der Button wird als klickbarer Link in die E-Mail eingefügt.</p>
      <label>
        Beschriftung
        <input name="label" type="text" maxlength="80" required placeholder="Zum Angebot">
      </label>
      <label>
        Ziel-URL
        <input name="url" type="url" required placeholder="https://example.de/angebot">
      </label>
      <p class="email-editor-button-dialog-error" data-email-button-error hidden></p>
      <div class="dialog-actions">
        <button class="button button-secondary" type="button" data-email-button-cancel>Abbrechen</button>
        <button class="button" type="submit">Einfügen</button>
      </div>
    </form>`;

  const form = dialog.querySelector('form');
  const labelInput = dialog.querySelector('input[name="label"]');
  const urlInput = dialog.querySelector('input[name="url"]');
  const error = dialog.querySelector('[data-email-button-error]');
  const cancel = dialog.querySelector('[data-email-button-cancel]');
  const showError = (message) => {
    error.textContent = message;
    error.hidden = false;
  };
  const clearError = () => {
    error.textContent = '';
    error.hidden = true;
  };

  cancel.addEventListener('click', () => dialog.close());
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const label = labelInput.value.trim();
    const url = normalizeButtonUrl(urlInput.value);
    if (!label) {
      showError('Bitte eine Beschriftung eingeben.');
      labelInput.focus();
      return;
    }
    if (!url) {
      showError('Bitte eine gültige http-, https-, mailto- oder tel-Adresse eingeben.');
      urlInput.focus();
      return;
    }

    clearError();
    editor.s.restore();
    editor.s.insertHTML(emailButtonMarkup(label, url));
    editor.e.fire('change');
    dialog.close();
  });
  dialog.addEventListener('close', () => dialog.remove(), { once: true });

  document.body.append(dialog);
  dialog.showModal();
  labelInput.focus();
}

function valueFieldFor(host) {
  return host.closest('label')?.querySelector('[data-email-compose-content-value]') || null;
}

function fitMobilePreview(editor, host) {
  const viewport = host.closest('[data-template-editor-viewport]');
  const body = editor?.editor;
  const frame = editor?.iframe;
  if (!body || !frame) return;

  body.style.zoom = '';
  if (viewport?.dataset.templateEditorView !== 'mobile') return;

  // Mail apps fit legacy fixed-width table layouts into the phone viewport.
  const availableWidth = frame.clientWidth;
  const contentWidth = Math.max(body.scrollWidth, body.offsetWidth);
  if (!availableWidth || contentWidth <= availableWidth) return;
  body.style.zoom = String(availableWidth / contentWidth);
}

function refreshEditor(editor, host) {
  if (!editor) return;
  window.requestAnimationFrame(() => {
    fitMobilePreview(editor, host);
    editor.e.fire('resize');
    const frameWindow = editor.iframe?.contentWindow;
    if (frameWindow) frameWindow.dispatchEvent(new frameWindow.Event('resize'));
  });
}

function initializeEditor(host) {
  const valueField = valueFieldFor(host);
  if (!valueField || editors.has(host)) return;

  const defaults = editorDefaults();
  let editor;
  const sync = () => {
    if (!editor) return;
    valueField.value = editor.value;
    host.dispatchEvent(new Event('input', { bubbles: true }));
    refreshEditor(editor, host);
  };

  editor = Jodit.make(host, {
    language: 'de',
    buttons: toolbar,
    buttonsMD: toolbar,
    buttonsSM: toolbar,
    buttonsXS: toolbar,
    toolbarAdaptive: false,
    toolbarSticky: false,
    showCharsCounter: false,
    showWordsCounter: false,
    showXPathInStatusbar: false,
    iframe: true,
    iframeStyle: emailDocumentStyle(defaults),
    enter: 'br',
    controls: {
      localImage: {
        icon: 'image',
        tooltip: 'Bild vom Computer einfügen',
        exec(activeEditor) {
          chooseLocalImage(activeEditor, host);
          return false;
        },
      },
      emailButton: {
        icon: 'email-button',
        tooltip: 'Klickbaren E-Mail-Button einfügen',
        exec(activeEditor) {
          openButtonDialog(activeEditor);
          return false;
        },
      },
    },
    askBeforePasteHTML: false,
    askBeforePasteFromWord: false,
    beautifyHTML: false,
    disablePlugins: ['clean-html'],
    events: { change: sync },
  });
  editor.value = valueField.value || host.value || '';
  editor.events.on('blur', sync);
  editors.set(host, editor);
  sync();
}

function initializeEditors() {
  document.querySelectorAll('[data-email-rich-editor] [data-email-compose-content]').forEach(initializeEditor);
}

window.KosmosEmailEditors = {
  getHTML(host) {
    return editors.get(host)?.value || host?.value || '';
  },
  setHTML(host, html) {
    const editor = editors.get(host);
    if (editor) {
      editor.value = html || '';
      return;
    }
    if (host) host.value = html || '';
  },
  clear(host) {
    this.setHTML(host, '');
  },
  refresh(host) {
    const editor = editors.get(host);
    refreshEditor(editor, host);
  },
};

initializeEditors();
