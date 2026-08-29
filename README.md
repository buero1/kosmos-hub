# kosmos-hub

Monorepo fuer die zentrale Kosmos-Plattform und das zugehoerige WordPress-Plugin
`Kosmos Bridge`.

## Struktur

- `docs/`: Architektur-, Sicherheits- und Umsetzungsdokumentation
- `server/`: FastAPI-Backend, Datenmodell und einfache MVP-Weboberflaeche
- `wordpress-plugin/`: WordPress-Plugin fuer Registrierung, Heartbeat und spaetere MCP-Anbindung

## Release-Strategie

- `Kosmos Bridge` bleibt Teil dieses Monorepos.
- Das Plugin liefert seine Updates spaeter ueber die normale WordPress-Updateansicht.
- Die Update-Dateien liegen unter `https://plugins.kosmos-medien.de/kosmos-bridge/`.
- Der Release-Workflow liegt in `.github/workflows/release-kosmos-bridge.yml`.
- Plugin-Releases werden ueber Git-Tags im Format `bridge-vX.Y.Z` ausgeloest.
- Der Betriebsablauf fuer Hub-Releases steht in [docs/release-playbook.md](docs/release-playbook.md).

## Phase 1

Phase 1 liefert das Fundament:

- zentrale Site-Registry
- per-Site-Registrierung ueber `Kosmos Bridge`
- verschluesselte Speicherung von Site-Secrets
- Audit-Logging fuer Registrierungs- und Heartbeat-Aktionen
- einfache Verwaltungsoberflaeche ohne komplexes Login

## Naechste Meilensteine

1. Test-WordPress mit `Kosmos Bridge` verbinden
2. Registrierung gegen `kosmos-hub` pruefen
3. Phase 2: Site-MCP und Kosmos-MCP anbinden
