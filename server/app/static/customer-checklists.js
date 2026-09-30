(function () {
  "use strict";

  function start() {
    var root = document.querySelector("[data-customer-checklists]");
    if (!root) return;

    function activeId(element) {
      var card = element && element.closest("[data-checklist-id]");
      return card ? card.getAttribute("data-checklist-id") : "";
    }

    function expandedChecklistId() {
      var toggle = root.querySelector('[data-checklist-toggle][aria-expanded="true"]');
      return activeId(toggle);
    }

    function showError(message) {
      var error = root.querySelector("[data-customer-checklist-error]");
      if (!error) return;
      error.textContent = message || "Die Checkliste konnte nicht gespeichert werden.";
      error.hidden = false;
    }

    function replace(html) {
      var shell = document.createElement("div");
      shell.innerHTML = html;
      var next = shell.firstElementChild;
      if (!next) throw new Error("Die Checkliste konnte nicht neu geladen werden.");
      root.replaceWith(next);
      root = next;
      bind();
    }

    function submit(action, values) {
      if (root.classList.contains("is-saving")) return Promise.resolve();
      root.classList.add("is-saving");
      var error = root.querySelector("[data-customer-checklist-error]");
      if (error) error.hidden = true;
      var form = new FormData();
      form.set("csrf_token", root.getAttribute("data-customer-checklists-csrf") || "");
      form.set("action", action);
      Object.keys(values || {}).forEach(function (key) { form.set(key, values[key]); });
      return window.fetch(root.getAttribute("data-customer-checklists-url"), {
        method: "POST",
        body: form,
        credentials: "same-origin"
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          if (!response.ok) throw new Error(payload.detail || "Die Checkliste konnte nicht gespeichert werden.");
          return payload;
        });
      }).then(function (payload) {
        replace(payload.html);
      }).catch(function (errorValue) {
        root.classList.remove("is-saving");
        showError(errorValue.message);
      });
    }

    function openOnly(card) {
      root.querySelectorAll("[data-checklist-id]").forEach(function (candidate) {
        var open = candidate === card;
        var toggle = candidate.querySelector("[data-checklist-toggle]");
        var panel = candidate.querySelector("[data-checklist-panel]");
        if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (panel) panel.hidden = !open;
      });
    }

    function showForm(form) {
      if (!form) return;
      form.hidden = false;
      var input = form.querySelector("input[type=text]");
      if (input) {
        input.focus();
        input.select();
      }
    }

    function bindSortables() {
      if (!window.Sortable || !root.querySelector(".customer-checklist-drag")) return;
      var list = root.querySelector("[data-customer-checklist-list]");
      if (list) {
        window.Sortable.create(list, {
          animation: 150,
          draggable: "[data-checklist-id]",
          handle: ".customer-checklist-drag",
          ghostClass: "customer-checklist-sortable-ghost",
          chosenClass: "customer-checklist-sortable-chosen",
          onEnd: function () {
            var ids = Array.prototype.map.call(list.querySelectorAll(":scope > [data-checklist-id]"), function (card) {
              return Number(card.getAttribute("data-checklist-id"));
            });
            submit("checklist.reorder", { order_json: JSON.stringify(ids), active_id: expandedChecklistId() });
          }
        });
      }
      root.querySelectorAll("[data-checklist-items]").forEach(function (items) {
        window.Sortable.create(items, {
          animation: 150,
          draggable: "[data-checklist-item-id]",
          handle: ".customer-checklist-item-drag",
          ghostClass: "customer-checklist-sortable-ghost",
          chosenClass: "customer-checklist-sortable-chosen",
          onEnd: function () {
            var checklistId = activeId(items);
            var ids = Array.prototype.map.call(items.querySelectorAll(":scope > [data-checklist-item-id]"), function (item) {
              return Number(item.getAttribute("data-checklist-item-id"));
            });
            submit("item.reorder", { checklist_id: checklistId, active_id: checklistId, order_json: JSON.stringify(ids) });
          }
        });
      });
    }

    function bind() {
      root.classList.remove("is-saving");
      bindSortables();
    }

    document.addEventListener("click", function (event) {
      var target = event.target;
      var toggle = target.closest("[data-checklist-toggle]");
      if (toggle) {
        var card = toggle.closest("[data-checklist-id]");
        var panel = card && card.querySelector("[data-checklist-panel]");
        var opening = Boolean(panel && panel.hidden);
        if (opening) openOnly(card);
        else if (panel) {
          panel.hidden = true;
          toggle.setAttribute("aria-expanded", "false");
        }
        return;
      }
      var editList = target.closest("[data-checklist-edit-open]");
      if (editList) {
        var editCard = editList.closest("[data-checklist-id]");
        openOnly(editCard);
        showForm(editCard.querySelector('[data-checklist-form-action="checklist.update"]'));
        return;
      }
      var deleteList = target.closest("[data-checklist-delete]");
      if (deleteList) {
        var deleteCard = deleteList.closest("[data-checklist-id]");
        var title = deleteCard.querySelector(".customer-checklist-title").textContent.trim();
        if (window.confirm('Checkliste "' + title + '" einschließlich aller Punkte löschen?')) {
          submit("checklist.delete", { checklist_id: activeId(deleteCard) });
        }
        return;
      }
      var editItem = target.closest("[data-checklist-item-edit-open]");
      if (editItem) {
        showForm(editItem.closest("[data-checklist-item-id]").querySelector("[data-checklist-form]"));
        return;
      }
      var deleteItem = target.closest("[data-checklist-item-delete]");
      if (deleteItem) {
        var item = deleteItem.closest("[data-checklist-item-id]");
        var itemCard = item.closest("[data-checklist-id]");
        submit("item.delete", {
          checklist_id: activeId(itemCard),
          active_id: activeId(itemCard),
          item_id: item.getAttribute("data-checklist-item-id")
        });
        return;
      }
      var addItem = target.closest("[data-checklist-item-add-open]");
      if (addItem) {
        showForm(addItem.nextElementSibling);
        return;
      }
      var addList = target.closest("[data-checklist-add-open]");
      if (addList) {
        showForm(root.querySelector('[data-checklist-form-action="checklist.create"]'));
        return;
      }
      var cancel = target.closest("[data-checklist-form-cancel]");
      if (cancel) {
        var cancelled = cancel.closest("[data-checklist-form]");
        cancelled.reset();
        cancelled.hidden = true;
      }
    });

    document.addEventListener("change", function (event) {
      var checkbox = event.target.closest("[data-checklist-item-toggle]");
      if (!checkbox) return;
      var item = checkbox.closest("[data-checklist-item-id]");
      var card = item.closest("[data-checklist-id]");
      submit("item.toggle", {
        checklist_id: activeId(card),
        active_id: activeId(card),
        item_id: item.getAttribute("data-checklist-item-id")
      });
    });

    document.addEventListener("submit", function (event) {
      var form = event.target.closest("[data-checklist-form]");
      if (!form) return;
      event.preventDefault();
      var card = form.closest("[data-checklist-id]");
      var item = form.closest("[data-checklist-item-id]");
      var values = {};
      new FormData(form).forEach(function (value, key) { values[key] = String(value); });
      if (card) {
        values.checklist_id = activeId(card);
        values.active_id = activeId(card);
      }
      if (item) values.item_id = item.getAttribute("data-checklist-item-id");
      submit(form.getAttribute("data-checklist-form-action"), values);
    });

    bind();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
