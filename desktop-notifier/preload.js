const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("kosmosNotifier", {
  getState: function () { return ipcRenderer.invoke("notifier:get-state"); },
  saveConfig: function (config) { return ipcRenderer.invoke("notifier:save-config", config); },
  refresh: function () { return ipcRenderer.invoke("notifier:refresh"); },
  snooze: function (notificationIds, minutes) { return ipcRenderer.invoke("notifier:snooze", notificationIds, minutes); },
  snoozeBeforeStart: function (notificationIds, minutesBefore) { return ipcRenderer.invoke("notifier:snooze-before-start", notificationIds, minutesBefore); },
  complete: function (notificationIds) { return ipcRenderer.invoke("notifier:complete", notificationIds); },
  openCustomer: function (customerId) { return ipcRenderer.invoke("notifier:open-customer", customerId); },
  onUpdate: function (callback) { ipcRenderer.on("reminders:update", (_event, payload) => callback(payload)); },
  onError: function (callback) { ipcRenderer.on("reminders:error", (_event, message) => callback(message)); },
  onRefresh: function (callback) { ipcRenderer.on("reminders:refresh", callback); },
});
