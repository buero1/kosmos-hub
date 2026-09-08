const setup = document.querySelector("#setup");
const reminderArea = document.querySelector("#reminder-area");
const setupForm = document.querySelector("#setup-form");
const reminderList = document.querySelector("#reminder-list");
const emptyState = document.querySelector("#empty-state");
const message = document.querySelector("#message");
const selectAll = document.querySelector("#select-all");
const bulkSnooze = document.querySelector("#bulk-snooze");
const reminderCount = document.querySelector("#reminder-count");
const selectedReminderAppointment = document.querySelector("#selected-reminder-appointment");
let reminders = [];
let selectedReminderIds = new Set();
let isConfigured = false;
let snoozeSelectionContext = "";
let snoozeSelectionWasChanged = false;

function showConfiguredView() {
  isConfigured = true;
  setup.hidden = true;
  reminderArea.hidden = false;
}

function showSetupView() {
  if (isConfigured) return;
  setup.hidden = false;
  reminderArea.hidden = true;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, function (character) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character];
  });
}

function setMessage(text, isError = false) {
  message.textContent = text || "";
  message.classList.toggle("error", isError);
}

function selectedIds() {
  return [...selectedReminderIds];
}

function overdueSince(startsAt) {
  const startTime = new Date(startsAt).getTime();
  if (!Number.isFinite(startTime)) return "Fällig";

  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - startTime) / 60_000));
  if (elapsedMinutes === 0) return "Jetzt fällig";

  const days = Math.floor(elapsedMinutes / 1440);
  const hours = Math.floor((elapsedMinutes % 1440) / 60);
  const minutes = elapsedMinutes % 60;
  const parts = [];
  if (days) parts.push(`${days} ${days === 1 ? "Tag" : "Tagen"}`);
  if (hours) parts.push(`${hours} ${hours === 1 ? "Stunde" : "Stunden"}`);
  if (minutes) parts.push(`${minutes} ${minutes === 1 ? "Minute" : "Minuten"}`);
  return `Seit ${parts.join(" ")} fällig`;
}

function formatAppointment(startsAt) {
  const appointment = new Date(startsAt);
  if (Number.isNaN(appointment.getTime())) return "";
  return new Intl.DateTimeFormat("de-DE", { dateStyle: "medium", timeStyle: "short" }).format(appointment);
}

function render(payload) {
  // A successful reminder update confirms that the saved device pairing is usable.
  showConfiguredView();
  reminders = payload.reminders || [];
  const availableIds = new Set(reminders.map(function (reminder) { return reminder.id; }));
  selectedReminderIds = new Set([...selectedReminderIds].filter(function (id) { return availableIds.has(id); }));
  reminderList.innerHTML = reminders.map(function (reminder) {
    const selected = selectedReminderIds.has(reminder.id);
    const hubLink = reminder.customer_id
      ? `<button class="hub-link" type="button" data-row-open-button data-customer-id="${reminder.customer_id}">Im Hub öffnen</button>`
      : "";
    return `<article class="reminder${selected ? " is-selected" : ""}" data-reminder-row data-id="${reminder.id}" tabindex="0" role="button" aria-pressed="${selected}" aria-label="${escapeHtml(reminder.activity_name)} auswählen">
      <input class="reminder-select" type="checkbox" value="${reminder.id}" data-reminder-select aria-label="${escapeHtml(reminder.activity_name)} auswählen"${selected ? " checked" : ""}>
      <h2>${escapeHtml(reminder.activity_name)}</h2>
      <div class="reminder-meta">
        <span>${overdueSince(reminder.starts_at)}</span>
        ${hubLink}
      </div>
    </article>`;
  }).join("");
  emptyState.hidden = reminders.length !== 0;
  document.querySelector(".bulk-bar").hidden = reminders.length === 0;
  updateSelectionControl();
}

function updateSelectionControl() {
  const selectedCount = selectedReminderIds.size;
  selectAll.checked = reminders.length > 0 && selectedCount === reminders.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < reminders.length;
  const selectedReminder = selectedCount === 1
    ? reminders.find(function (reminder) { return selectedReminderIds.has(reminder.id); })
    : null;
  const titleCount = selectedCount || reminders.length;
  reminderCount.textContent = selectedReminder
    ? selectedReminder.activity_name
    : `${titleCount} ${titleCount === 1 ? "Erinnerung" : "Erinnerungen"}`;
  const appointment = selectedReminder ? formatAppointment(selectedReminder.starts_at) : "";
  selectedReminderAppointment.hidden = !appointment;
  selectedReminderAppointment.textContent = appointment ? `Termin: ${appointment} Uhr` : "";
  updateSnoozeSelection();
}

function snoozeContextReminders() {
  const selected = reminders.filter(function (reminder) { return selectedReminderIds.has(reminder.id); });
  return selected.length ? selected : reminders;
}

function recommendedSnoozeValue(remindersForContext) {
  if (remindersForContext.length !== 1) return "10";
  const reminder = remindersForContext[0];
  if (reminder.activity_kind !== "call" && reminder.activity_kind !== "meeting") return "10";
  const minutesUntilStart = (new Date(reminder.starts_at).getTime() - Date.now()) / 60_000;
  if (!Number.isFinite(minutesUntilStart) || minutesUntilStart <= 0) return "10";
  return minutesUntilStart > 5 ? "before-start:5" : "before-start:0";
}

function updateSnoozeSelection() {
  const remindersForContext = snoozeContextReminders();
  const context = remindersForContext.map(function (reminder) { return reminder.id; }).join(",");
  if (context !== snoozeSelectionContext) snoozeSelectionWasChanged = false;
  snoozeSelectionContext = context;
  if (!snoozeSelectionWasChanged) bulkSnooze.value = recommendedSnoozeValue(remindersForContext);
}

function toggleReminderSelection(id) {
  if (selectedReminderIds.has(id)) selectedReminderIds.delete(id);
  else selectedReminderIds.add(id);
  render({ reminders });
}

async function act(action) {
  try {
    await action();
    setMessage("");
  } catch (error) {
    setMessage(error.message || "Die Aktion konnte nicht ausgeführt werden.", true);
  }
}

setupForm.addEventListener("submit", async function (event) {
  event.preventDefault();
  await act(async function () {
    await window.kosmosNotifier.saveConfig({
      serverUrl: document.querySelector("#server-url").value,
      token: document.querySelector("#device-token").value,
      launchAtLogin: document.querySelector("#launch-at-login").checked,
    });
    showConfiguredView();
    setMessage("Windows-Gerät verbunden.");
  });
});

selectAll.addEventListener("change", function () {
  selectedReminderIds = selectAll.checked
    ? new Set(reminders.map(function (reminder) { return reminder.id; }))
    : new Set();
  render({ reminders });
});

document.querySelector("#bulk-snooze-button").addEventListener("click", function () {
  const ids = selectedIds();
  if (!ids.length) return setMessage("Bitte mindestens eine Erinnerung auswählen.", true);
  const beforeStart = /^before-start:(0|5)$/.exec(bulkSnooze.value);
  act(function () {
    return beforeStart
      ? window.kosmosNotifier.snoozeBeforeStart(ids, Number(beforeStart[1]))
      : window.kosmosNotifier.snooze(ids, Number(bulkSnooze.value));
  });
});

bulkSnooze.addEventListener("change", function () {
  snoozeSelectionWasChanged = true;
});

document.querySelector("#bulk-complete-button").addEventListener("click", function () {
  const ids = selectedIds();
  if (!ids.length) return setMessage("Bitte mindestens eine Erinnerung auswählen.", true);
  act(function () { return window.kosmosNotifier.complete(ids); });
});

reminderList.addEventListener("click", function (event) {
  const openButton = event.target.closest("[data-row-open-button]");
  if (openButton) {
    event.stopPropagation();
    return window.kosmosNotifier.openCustomer(Number(openButton.dataset.customerId));
  }
  if (event.target.closest("[data-reminder-select]")) return;
  const row = event.target.closest("[data-reminder-row]");
  if (row) toggleReminderSelection(Number(row.dataset.id));
});

reminderList.addEventListener("change", function (event) {
  const input = event.target.closest("[data-reminder-select]");
  if (!input) return;
  const id = Number(input.value);
  if (input.checked) selectedReminderIds.add(id);
  else selectedReminderIds.delete(id);
  render({ reminders });
});

reminderList.addEventListener("keydown", function (event) {
  if (event.key !== "Enter" && event.key !== " ") return;
  if (event.target.closest("[data-row-open-button], [data-reminder-select]")) return;
  const row = event.target.closest("[data-reminder-row]");
  if (!row) return;
  event.preventDefault();
  toggleReminderSelection(Number(row.dataset.id));
});

window.kosmosNotifier.onUpdate(render);
window.kosmosNotifier.onError(function (error) { setMessage(error, true); });
window.kosmosNotifier.onRefresh(function () { window.kosmosNotifier.refresh(); });

window.kosmosNotifier.getState().then(function (state) {
  if (state.configured) {
    showConfiguredView();
    document.querySelector("#server-url").value = state.serverUrl;
    document.querySelector("#launch-at-login").checked = state.launchAtLogin;
  } else {
    showSetupView();
  }
});
