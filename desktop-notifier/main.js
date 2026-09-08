const { app, BrowserWindow, ipcMain, Menu, nativeImage, Notification, safeStorage, shell, Tray } = require("electron");
const fs = require("fs");
const path = require("path");

const POLL_INTERVAL_MS = 30_000;
let reminderWindow;
let tray;
let pollingTimer;
let lastReminderSignature = "";

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) app.quit();

app.on("second-instance", showReminderWindow);

function configPath() {
  return path.join(app.getPath("userData"), "notifier-config.json");
}

function readConfig() {
  try {
    const stored = JSON.parse(fs.readFileSync(configPath(), "utf8"));
    if (!stored.serverUrl || !stored.token) return null;
    return {
      serverUrl: stored.serverUrl,
      token: safeStorage.isEncryptionAvailable()
        ? safeStorage.decryptString(Buffer.from(stored.token, "base64"))
        : "",
      launchAtLogin: Boolean(stored.launchAtLogin),
    };
  } catch {
    return null;
  }
}

function saveConfig({ serverUrl, token, launchAtLogin }) {
  const normalizedUrl = normalizeHubUrl(serverUrl);
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error("Windows konnte den Kopplungscode nicht sicher speichern.");
  }
  const encryptedToken = safeStorage.encryptString(String(token).trim()).toString("base64");
  const config = { serverUrl: normalizedUrl, token: encryptedToken, launchAtLogin: Boolean(launchAtLogin) };
  fs.mkdirSync(path.dirname(configPath()), { recursive: true });
  fs.writeFileSync(configPath(), JSON.stringify(config), { encoding: "utf8", mode: 0o600 });
  app.setLoginItemSettings({ openAtLogin: config.launchAtLogin });
  return { serverUrl: config.serverUrl, launchAtLogin: config.launchAtLogin };
}

function normalizeHubUrl(value) {
  const parsed = new URL(String(value).trim());
  const localHttp = parsed.protocol === "http:" && ["localhost", "127.0.0.1"].includes(parsed.hostname);
  if (parsed.protocol !== "https:" && !localHttp) {
    throw new Error("Der Hub muss per HTTPS erreichbar sein.");
  }
  return parsed.origin;
}

function createReminderWindow() {
  reminderWindow = new BrowserWindow({
    width: 512,
    height: 445,
    minWidth: 440,
    minHeight: 330,
    show: false,
    alwaysOnTop: true,
    autoHideMenuBar: true,
    title: "Kosmos Erinnerungen",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  reminderWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  reminderWindow.webContents.on("did-finish-load", pollReminders);
  reminderWindow.on("close", function (event) {
    if (!app.isQuitting) {
      event.preventDefault();
      reminderWindow.hide();
    }
  });
}

function createTray() {
  const icon = nativeImage.createFromDataURL("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iOCIgcmlsbD0iIzFlNTg5OCIvPjxwYXRoIGQ9Ik0xNiA4YTEwIDEwIDAgMSAwIDEwIDEwQTExLjUgMTEuNSAwIDAgMSAxNiA4em0wIDN2N2w1IDMiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIyLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjwvc3ZnPg==");
  tray = new Tray(icon);
  tray.setToolTip("Kosmos Erinnerungen");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Erinnerungen anzeigen", click: showReminderWindow },
    { type: "separator" },
    { label: "Beenden", click: function () { app.isQuitting = true; app.quit(); } },
  ]));
  tray.on("double-click", showReminderWindow);
}

function showReminderWindow() {
  if (!reminderWindow) return;
  reminderWindow.show();
  reminderWindow.focus();
  reminderWindow.webContents.send("reminders:refresh");
}

async function apiRequest(pathname, options = {}) {
  const config = readConfig();
  if (!config || !config.token) throw new Error("Bitte zuerst Hub-Adresse und Kopplungscode eingeben.");
  const response = await fetch(`${config.serverUrl}${pathname}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${config.token}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || "Die Verbindung zum Hub ist fehlgeschlagen.");
  }
  return response.json();
}

async function pollReminders({ hideWhenEmpty = false } = {}) {
  try {
    const payload = await apiRequest("/api/v1/desktop/reminders");
    const reminders = payload.reminders || [];
    const hadDueReminders = Boolean(lastReminderSignature);
    reminderWindow.webContents.send("reminders:update", payload);
    const signature = reminders.map((reminder) => reminder.id).join(",");
    if (signature && signature !== lastReminderSignature) {
      const notification = new Notification({
        title: "Kosmos Erinnerungen",
        body: reminders.length === 1
          ? `Fällig: ${reminders[0].activity_name}`
          : `${reminders.length} Aktivitäten warten auf Sie.`,
      });
      notification.on("click", showReminderWindow);
      notification.show();
      showReminderWindow();
    }
    lastReminderSignature = signature;
    if ((hideWhenEmpty || hadDueReminders) && !reminders.length && reminderWindow.isVisible()) reminderWindow.hide();
  } catch (error) {
    reminderWindow.webContents.send("reminders:error", error.message);
  }
}

app.whenReady().then(function () {
  createReminderWindow();
  createTray();
  pollingTimer = setInterval(pollReminders, POLL_INTERVAL_MS);
  pollReminders();
  if (!readConfig()) showReminderWindow();
  app.on("activate", showReminderWindow);
});

app.on("window-all-closed", function () {
  // The tray application continues receiving reminders when all windows are hidden.
});

app.on("before-quit", function () {
  app.isQuitting = true;
  if (pollingTimer) clearInterval(pollingTimer);
});

ipcMain.handle("notifier:get-state", function () {
  const config = readConfig();
  return config ? { configured: true, serverUrl: config.serverUrl, launchAtLogin: config.launchAtLogin } : { configured: false };
});

ipcMain.handle("notifier:save-config", async function (_event, config) {
  const state = saveConfig(config);
  await pollReminders();
  return state;
});

ipcMain.handle("notifier:snooze", async function (_event, notificationIds, minutes) {
  const result = await apiRequest("/api/v1/desktop/reminders/snooze", {
    method: "POST",
    body: JSON.stringify({ notification_ids: notificationIds, minutes }),
  });
  await pollReminders({ hideWhenEmpty: true });
  return result;
});

ipcMain.handle("notifier:snooze-before-start", async function (_event, notificationIds, minutesBefore) {
  const result = await apiRequest("/api/v1/desktop/reminders/snooze-before-start", {
    method: "POST",
    body: JSON.stringify({ notification_ids: notificationIds, minutes_before: minutesBefore }),
  });
  await pollReminders({ hideWhenEmpty: true });
  return result;
});

ipcMain.handle("notifier:refresh", async function () {
  await pollReminders();
});

ipcMain.handle("notifier:complete", async function (_event, notificationIds) {
  const result = await apiRequest("/api/v1/desktop/reminders/complete", {
    method: "POST",
    body: JSON.stringify({ notification_ids: notificationIds }),
  });
  await pollReminders({ hideWhenEmpty: true });
  return result;
});

ipcMain.handle("notifier:open-customer", function (_event, customerId) {
  const config = readConfig();
  if (config) shell.openExternal(`${config.serverUrl}/customers/${customerId}#customer-activities`);
});
