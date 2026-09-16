(function () {
  var bar = document.querySelector("[data-hub-quick-access]");
  if (!bar) return;

  var search = bar.querySelector("[data-hub-quick-search]");
  var input = bar.querySelector("[data-hub-quick-search-input]");
  var results = bar.querySelector("[data-hub-quick-search-results]");
  var createMenu = bar.querySelector("[data-hub-quick-create]");
  var timer = null;
  var pending = null;

  function closeResults() {
    window.clearTimeout(timer);
    if (pending) pending.abort();
    results.hidden = true;
    input.setAttribute("aria-expanded", "false");
  }

  function options() {
    return Array.from(results.querySelectorAll("a[role='option']"));
  }

  function showMessage(message) {
    results.replaceChildren();
    var paragraph = document.createElement("p");
    paragraph.className = "hub-quick-search-empty";
    paragraph.textContent = message;
    results.appendChild(paragraph);
    results.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function render(groups) {
    results.replaceChildren();
    var count = 0;
    groups.forEach(function (group) {
      if (!Array.isArray(group.items) || !group.items.length) return;
      var section = document.createElement("section");
      section.className = "hub-quick-search-group";
      section.setAttribute("role", "group");
      section.setAttribute("aria-label", group.label);
      var heading = document.createElement("p");
      heading.className = "hub-quick-search-group-heading";
      heading.textContent = group.label;
      section.appendChild(heading);
      group.items.forEach(function (item) {
        var link = document.createElement("a");
        link.href = item.url;
        link.setAttribute("role", "option");
        link.id = "hub-quick-result-" + count;
        var title = document.createElement("strong");
        title.textContent = item.label;
        link.appendChild(title);
        if (item.detail) {
          var detail = document.createElement("small");
          detail.textContent = item.detail;
          link.appendChild(detail);
        }
        section.appendChild(link);
        count += 1;
      });
      results.appendChild(section);
    });
    if (!count) {
      showMessage("Keine Treffer");
      return;
    }
    results.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  async function loadResults() {
    var query = input.value.trim();
    if (query.length < 2) {
      closeResults();
      return;
    }
    pending = new AbortController();
    try {
      var response = await fetch("/search/suggestions?q=" + encodeURIComponent(query), {
        signal: pending.signal,
        headers: { "Accept": "application/json" }
      });
      if (!response.ok) throw new Error("Search failed");
      var payload = await response.json();
      if (input.value.trim() === query) render(Array.isArray(payload.groups) ? payload.groups : []);
    } catch (error) {
      if (error.name !== "AbortError" && input.value.trim() === query) {
        showMessage("Suche derzeit nicht verfuegbar");
      }
    }
  }

  input.addEventListener("input", function () {
    window.clearTimeout(timer);
    if (pending) pending.abort();
    if (input.value.trim().length < 2) {
      closeResults();
      return;
    }
    timer = window.setTimeout(loadResults, 220);
  });
  input.addEventListener("focus", function () {
    if (input.value.trim().length >= 2) {
      window.clearTimeout(timer);
      timer = window.setTimeout(loadResults, 120);
    }
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeResults();
      input.blur();
    } else if (event.key === "ArrowDown" && options().length) {
      event.preventDefault();
      options()[0].focus();
    } else if (event.key === "Enter" && options().length && !results.hidden) {
      event.preventDefault();
      options()[0].click();
    }
  });
  results.addEventListener("keydown", function (event) {
    var links = options();
    var index = links.indexOf(document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      closeResults();
      input.focus();
    } else if (event.key === "ArrowDown" && index >= 0) {
      event.preventDefault();
      links[(index + 1) % links.length].focus();
    } else if (event.key === "ArrowUp" && index >= 0) {
      event.preventDefault();
      if (index === 0) input.focus();
      else links[index - 1].focus();
    }
  });

  document.addEventListener("click", function (event) {
    if (!search.contains(event.target)) closeResults();
    if (createMenu && !createMenu.contains(event.target)) createMenu.open = false;
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && createMenu && createMenu.open) {
      createMenu.open = false;
      createMenu.querySelector("summary").focus();
    }
  });
  if (createMenu) {
    createMenu.addEventListener("toggle", function () {
      if (createMenu.open) closeResults();
    });
    var emailAction = createMenu.querySelector("[data-hub-quick-create-email]");
    emailAction.addEventListener("click", function (event) {
      createMenu.open = false;
      if (window.location.pathname === "/emails") {
        event.preventDefault();
        event.stopPropagation();
        var mailboxAction = document.querySelector("[data-mailbox-email-compose-open]");
        if (mailboxAction) mailboxAction.click();
      }
    });
  }

  function openRequestedCalendarForm() {
    if (window.location.pathname !== "/calendar") return;
    var url = new URL(window.location.href);
    var kind = url.searchParams.get("create");
    if (kind !== "call" && kind !== "task" && kind !== "meeting") return;
    url.searchParams.delete("create");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
    document.dispatchEvent(new CustomEvent("calendar:activity-open", { detail: { kind: kind } }));
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", openRequestedCalendarForm, { once: true });
  } else {
    openRequestedCalendarForm();
  }
}());
