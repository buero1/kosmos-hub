(function () {
  var unknownStatus = 'Der Versandstatus konnte nicht bestätigt werden. Deine Eingaben bleiben erhalten. Bitte vor erneutem Senden im Ordner Gesendet prüfen.';

  window.KosmosEmailDelivery = {
    navigate: function (url) {
      var target = new URL(url, window.location.href);
      var current = new URL(window.location.href);
      if (target.origin !== current.origin) throw new Error(unknownStatus);
      if (target.pathname === current.pathname && target.search === current.search) {
        // Fragment-only navigation does not reload a second successful send on the same record.
        window.history.replaceState(window.history.state, '', target.href);
        window.location.reload();
      } else {
        window.location.assign(target.href);
      }
    },
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
