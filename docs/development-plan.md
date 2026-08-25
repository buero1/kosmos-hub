# Kosmos Hub Development Plan

Stand: 2026-08-24

## Ziel fuer den naechsten Build-Zyklus

Ein kleines, stabiles Fundament schaffen, das wir gegen die Test-Site
`https://test-gasthofloewen.kosmos-medien.de/` verproben koennen.

## Phase 1

### Lieferumfang

- Monorepo-Struktur fuer `server` und `wordpress-plugin`
- FastAPI-Backend mit:
  - Health-Endpoint
  - Site-Registrierung
  - Site-Liste / Site-Detail
  - einfacher Dashboard-Webseite
- SQLAlchemy-Modelle fuer:
  - `customers`
  - `sites`
  - `site_connections`
  - `audit_log`
- `Kosmos Bridge`-Plugin mit:
  - Aktivierungslogik
  - `site_uuid` / `site_secret`
  - Registrierungsclient
  - Heartbeat-Cron
  - Admin-Statusseite

### Akzeptanzkriterien

1. `kosmos-hub` startet lokal mit konfigurierter Datenbankverbindung.
2. Eine Registrierung kann per HTTP-POST angenommen und gespeichert werden.
3. Die Site erscheint in der Uebersicht.
4. Registrierung und Heartbeat erzeugen Audit-Eintraege.
5. Das WordPress-Plugin kann auf der Test-Site installiert werden.

## Phase 2

### Ziel

Erste echte MCP-Verbindung:

```text
Codex -> kosmos-hub MCP -> Site MCP -> WordPress Ability
```

### Vorarbeiten

- offizielles Python-MCP-SDK integrieren
- zentralen Kosmos-MCP-Server aufsetzen
- Site-MCP-Endpunkt im Plugin mit `wordpress/mcp-adapter` anbinden
- erste Read-only-Ability discovery und execution

## Phase 3

- Site-Capability-Discovery
- Mapping von konkreten Abilities auf abstrakte Capabilities
- Speicherung der Discovery-Ergebnisse

Stand im aktuellen Build:

- zentrale Speicherung entdeckter Site-Abilities als erstes Inventar
- Hub-Refresh fuer gespeicherte Capability-Discovery
- getrennte Live-Discovery und gespeicherte Inventory-Ansicht
- erster echter State-Refresh fuer WordPress-/PHP-Versionen und aktive Plugins
- Speicherung des technischen Zustands als `site_snapshot`

## Phase 4

- offizielle WordPress-Update-Operationen
- Audit und Sicherheits-Gates fuer schreibende Aktionen
- vorbereitende Healthchecks / Smoke-Checks

Stand im aktuellen Build:

- zentrale, rein lesende Update-Inventur fuer WordPress, Plugins und Themes
- admin-context Update-Check fuer Anbieter, die ihre Update-Angebote nur in
  `wp-admin` registrieren
- Update-Workbench mit Versionsvergleich, Plugin-Aktivstatus und Filtern
- persistierbare Update-Entwuerfe mit unveraenderlichem Versionsbezug und
  Audit-Eintraegen
- Preflight fuer verifizierte Site, weiterhin verfuegbares Update und
  Backupanbindung; ohne Backup bleibt Ausfuehrung blockiert
- noch keine ausfuehrbaren Update-Aktionen, keine automatische Installation
  und keine persistierten Freigaben

## Phase 5

- Elementor- und andere vorhandene WordPress-Abilities untersuchen
- vorhandene Abilities bevorzugt wiederverwenden
- keine doppelte Implementierung ohne echten Grund

## Phase 6

- orchestrierte Wartungsablaeufe ueber mehrere Systeme
- Mittwald-/Zoho-Einbindung
- History, Wartungsruns und High-Level-Workflows

## Konkrete naechste Arbeitspakete

1. Server-Scaffold und Plugin-Scaffold fertigstellen
2. Datenbank und Env-Konfiguration fuers Server-Projekt finalisieren
3. erstes Test-Deployment fuer `kosmos-hub` vorbereiten
4. `Kosmos Bridge` auf der Test-Site installieren
5. erste echte Registrierung durchlaufen
6. Fehler und Onboarding-Reibung dokumentieren
