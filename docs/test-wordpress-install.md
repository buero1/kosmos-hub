# Test-WordPress Installation

Stand: 2026-08-24

## Zielsystem

- Test-Site: `https://test-gasthofloewen.kosmos-medien.de/`
- Zentrale Hub-URL: `https://kosmos-hub.31-70-92-95.sslip.io`

## Plugin-Paket

Fuer den aktuellen Test ist das Upload-Paket:

- `dist/kosmos-bridge-0.3.2.zip`

## Installation

1. Plugin-ZIP auf die Test-Site hochladen.
2. Wenn `Kosmos Bridge` bereits installiert ist, das Plugin mit diesem Paket aktualisieren.
3. Plugin aktivieren.
4. Etwa eine Minute warten oder die Plugin-Statusseite unter
   `Werkzeuge -> Kosmos Bridge` oeffnen.
5. Nur wenn die automatische Registrierung fehlschlaegt:
   `Retry registration now` ausloesen.

## Spaetere Updates

Sobald das Release auf `plugins.kosmos-medien.de` veroeffentlicht ist, kann
`Kosmos Bridge` wie ein normales WordPress-Plugin ueber die Update-Ansicht
aktualisiert werden. Die Site braucht dann keine manuelle Hub-Konfiguration.

Falls der Plugin-Host spaeter kurzzeitig stoert, kann `Kosmos Bridge` fuer die
Update-Metadaten ueber die GitHub-Release-API auf das neueste Release
ausweichen.

## Erwartetes Verhalten

Nach erfolgreicher Registrierung sollte die Test-Site in `kosmos-hub`
erscheinen:

- Dashboard: `/`
- API: `/api/v1/sites`

Lokaler Plugin-Status in WordPress:

- `Site UUID` gesetzt
- `Hub URL` = `https://kosmos-hub.31-70-92-95.sslip.io`
- `Status` = `ok`
- `Last success` gesetzt

## Hinweis zur ersten Phase

Das Plugin registriert in Phase 1 nur die Site selbst, Heartbeat und die
spaetere MCP-Ziel-URL. Die eigentliche WordPress-MCP-Execution kommt erst in
Phase 2.
