(function () {
  function downloadLink(filename, url, stored) {
    var link = document.createElement(url ? 'a' : 'span');
    link.textContent = filename + (stored ? ' (gespeichert)' : '');
    if (url) {
      link.href = url;
      link.download = filename;
      link.title = filename + ' herunterladen';
    }
    return link;
  }

  window.KosmosEmailDraftAttachments = {
    storedLink: function (attachment, draftId, scheduledId) {
      var emailId = String(scheduledId || draftId || '');
      var url = /^[1-9]\d*$/.test(emailId) && attachment.id
        ? '/emails/' + (scheduledId ? 'scheduled' : 'unassigned') + '/' + emailId + '/attachments/' + encodeURIComponent(attachment.id)
        : '';
      return downloadLink(attachment.filename, url, true);
    },
    localLink: function (file) {
      var url = window.URL.createObjectURL(file);
      var link = downloadLink(file.name, url, false);
      link.dataset.attachmentObjectUrl = url;
      return link;
    },
    clearList: function (list) {
      list.querySelectorAll('[data-attachment-object-url]').forEach(function (link) {
        window.URL.revokeObjectURL(link.dataset.attachmentObjectUrl);
      });
      list.replaceChildren();
    },
    sync: function (form, stored) {
      var field = form.elements.namedItem('retained_attachment_ids');
      if (field) field.value = JSON.stringify(stored.map(function (item) { return item.id; }));
    },
    capture: function (form, files) {
      var body = new FormData(form);
      body.delete('attachments');
      files.forEach(function (file) { body.append('attachments', file, file.name); });
      return { body: body, files: files.slice() };
    },
    reconcile: function (snapshot, files, stored, payload) {
      var ids = payload.uploaded_attachment_ids;
      if (!Array.isArray(payload.attachments) || !Array.isArray(ids) || ids.length !== snapshot.files.length ||
          ids.some(function (id) { return !payload.attachments.some(function (item) { return item.id === id; }); })) {
        throw new Error('Die Speicherung der Anh\u00e4nge konnte nicht best\u00e4tigt werden. Bitte erneut speichern.');
      }
      // Reconcile against the current selection, not the selection at upload time.
      // Files removed or added while saving must not reappear or be dropped.
      var retained = new Set(stored.map(function (item) { return item.id; }));
      snapshot.files.forEach(function (file, index) {
        if (files.includes(file)) retained.add(ids[index]);
      });
      return {
        stored: payload.attachments.filter(function (item) { return retained.has(item.id); }),
        files: files.filter(function (file) { return !snapshot.files.includes(file); })
      };
    }
  };
})();
