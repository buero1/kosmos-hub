(function () {
  var unknownStatus = 'Der Versandstatus konnte nicht bestätigt werden. Deine Eingaben bleiben erhalten. Bitte vor erneutem Senden im Ordner Gesendet prüfen.';

  window.KosmosEmailDelivery = {
    submit: function (form) {
      return window.fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: { Accept: 'application/json' }
      }).then(function (response) {
        return response.json().catch(function () {
          throw new Error(unknownStatus);
        }).then(function (payload) {
          if (!payload || typeof payload !== 'object') throw new Error(unknownStatus);
          if (!response.ok) {
            throw new Error(typeof payload.detail === 'string' ? payload.detail : unknownStatus);
          }
          if (typeof payload.redirect_url !== 'string' || !payload.redirect_url.startsWith('/') || payload.redirect_url.startsWith('//')) {
            throw new Error(unknownStatus);
          }
          return payload.redirect_url;
        });
      }, function () {
        // A lost response does not prove that SMTP rejected the message. Never retry automatically.
        throw new Error(unknownStatus);
      });
    }
  };
})();
