(function () {
  document.querySelectorAll('[data-offer-notes]').forEach(function (panel) {
    const editorPanel = panel.querySelector('[data-offer-notes-editor]');
    const host = editorPanel.querySelector('[data-email-compose-content]');
    const value = editorPanel.querySelector('[data-email-compose-content-value]');
    const form = value.form;
    const editors = window.KosmosEmailEditors;
    if (!form || !editors) return;
    function refresh() { if (panel.open && !editorPanel.hidden) editors.refresh(host); }
    panel.addEventListener('toggle', refresh);
    editorPanel.addEventListener('finance:position-edit-start', refresh);
    form.addEventListener('submit', function () { value.value = editors.getHTML(host); });
    form.addEventListener('reset', function () {
      window.requestAnimationFrame(function () { editors.setHTML(host, value.defaultValue); });
    });
  });
})();
