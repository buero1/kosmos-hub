# Kosmos Notifier

The Windows notifier is the first desktop client for Hub activity reminders.

## First local run

1. In the Hub, open `Account` and create a device under `Windows Erinnerungen`.
2. In this directory, run `npm install` and then `npm start`.
3. Enter the public Hub URL and the one-time pairing code in the setup window.

The application keeps running in the Windows tray. It polls the Hub for due `Popup` reminders every 30 seconds and opens one interactive reminder window for all due calls, meetings, and tasks. The window supports individual or selected bulk snoozing and completing.

The pairing code is encrypted with the Windows credential store by Electron and can be revoked at any time in the Hub account.

## Development start

On the development computer, double-click `Start-Kosmos-Notifier-Entwicklung.cmd`. It starts the app directly from the source files, so a restart after a code change takes seconds and does not require creating a new `.exe`.

## Windows build

Run `npm run dist` to create a portable Windows executable in `release/`. The resulting `.exe` can be started by double-clicking and does not need Node.js or npm on the target computer.
