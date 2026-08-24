# Entwicklungsauftrag: Kosmos MCP Plattform + WordPress Bridge

## 1. Ziel des Projekts

Es soll eine zentrale Plattform für die Verwaltung vieler WordPress-Kundenwebsites entstehen.

Die Plattform soll von Codex über MCP gesteuert werden können.

Langfristiges Ziel ist beispielsweise, folgende Anweisungen in natürlicher Sprache ausführen zu können:

- „Welche Kundenwebsites hatten seit sechs Monaten keine Wartung?“
- „Welche WordPress-Version läuft bei Kunde Müller?“
- „Welche Plugins haben dort Updates?“
- „Aktualisiere Elementor bei Kunde Müller.“
- „Erstelle vorher ein Backup und prüfe die Website danach.“
- „Ändere auf der Startseite von Kunde Müller den Text im Elementor-Widget.“
- „Zeige alle Kunden, bei denen Elementor Pro veraltet ist.“
- „Dokumentiere die durchgeführte Wartung.“
- „Prüfe bei Kunde X Mittwald, WordPress und Zoho und fasse den Zustand zusammen.“

Die Plattform soll für eine große Anzahl von Kundenwebsites ausgelegt sein.

Wichtig:

**Codex soll nicht 100 oder 200 einzelne WordPress-MCP-Verbindungen kennen und konfigurieren müssen.**

Stattdessen soll Codex hauptsächlich mit einem zentralen **Kosmos MCP Server** kommunizieren.

---

# 2. Zielarchitektur

Die gewünschte Architektur ist:

```text
                         CODEX
                           │
             ┌─────────────┼─────────────┐
             │ MCP         │ MCP         │ MCP
             ▼             ▼             ▼
        KOSMOS MCP     MITTWALD MCP    ZOHO MCP
             │
             │
             │ verwaltet/proxyt
             │ WordPress-Sites
             │
       ┌─────┼──────────────┐
       ▼     ▼              ▼
    Kunde A Kunde B       Kunde C
       │     │              │
   Kosmos  Kosmos         Kosmos
   Bridge  Bridge         Bridge
       │     │              │
       ▼     ▼              ▼
   WP MCP  WP MCP         WP MCP
       │
       ▼
 WordPress Abilities
       │
  ┌────┼───────────────┐
  ▼    ▼               ▼
Core  Elementor      Kosmos
      Abilities      Abilities
```

Dabei gilt:

### Codex

Codex ist der Agent.

Codex entscheidet anhand meiner natürlichsprachlichen Anweisung, welche Werkzeuge benötigt werden und in welcher Reihenfolge sie ausgeführt werden.

### Kosmos MCP Server

Der Kosmos MCP Server ist die zentrale Verwaltungsebene für alle Kundenwebsites.

Er enthält unter anderem:

- Kunden-/Website-Zuordnung
- Site Registry
- MCP-Endpunkte der Kundenwebsites
- Authentifizierungsinformationen
- Wartungshistorie
- bekannte Softwarestände
- letzte Prüfungen
- Statusinformationen
- Logs über ausgeführte Aktionen

Zusätzlich agiert der Kosmos Server als **MCP-Client/Gateway zu den WordPress-MCP-Servern der einzelnen Websites**.

Codex muss deshalb nicht jede einzelne Kundenwebsite als eigenen MCP-Server konfigurieren.

### Mittwald MCP und Zoho MCP

Wenn geeignete externe MCP-Server vorhanden sind, sollen diese direkt verwendet werden.

Funktionen von Mittwald und Zoho sollen nicht unnötig im Kosmos MCP nachgebaut werden.

Codex darf Tools verschiedener MCP-Server kombinieren.

Beispiel:

```text
Kosmos MCP
→ Kunde und Website bestimmen

Mittwald MCP
→ Hostingprojekt und Backup prüfen

Kosmos MCP
→ WordPress-Ability ausführen

Zoho MCP
→ Wartungsnotiz beim Kunden eintragen
```

---

# 3. Grundprinzip: Keine Funktionen doppelt entwickeln

Dies ist eine zentrale Architekturregel.

Wenn eine vorhandene WordPress Ability oder ein vorhandener MCP bereits eine Funktion bereitstellt, darf diese Funktion nicht ohne Grund erneut entwickelt werden.

Beispiel:

Wenn ein Elementor-Plugin bereits diese Abilities bereitstellt:

```text
get-page-structure
get-element-settings
add-container
insert-widget
update-element-settings
remove-element
```

sollen diese verwendet werden.

Nicht dieselben Elementor-Funktionen nochmals im Kosmos Bridge Plugin programmieren.

Eigene Kosmos-Abilities sollen nur dort entstehen, wo benötigte Funktionen fehlen oder eine stabile Kosmos-spezifische Abstraktion erforderlich ist.

---

# 4. Kosmos Bridge WordPress Plugin

Es soll ein eigenes kleines WordPress-Plugin entwickelt werden:

```text
Kosmos Bridge
```

Das Plugin wird später zentral über unsere vorhandene WordPress-Verwaltung auf viele Kundenwebsites installiert und aktiviert.

Daher muss die Installation möglichst vollständig automatisch erfolgen.

Nach der Masseninstallation soll kein manueller Login auf jeder einzelnen Kundenwebsite erforderlich sein.

---

# 5. Aufgaben des Kosmos Bridge Plugins

Das Plugin soll NICHT WordPress, Elementor oder andere Plugins komplett nachbauen.

Seine Hauptaufgaben sind:

## 5.1 WordPress MCP Infrastruktur

Das Plugin soll den offiziellen WordPress MCP Adapter verwenden.

Bevorzugt als Composer-Abhängigkeit:

```text
wordpress/mcp-adapter
```

Die Abhängigkeit soll sauber gekapselt werden.

Auf Versionskonflikte mit anderen Plugins, die ebenfalls den MCP Adapter verwenden, muss geachtet werden.

---

## 5.2 Eigener Kosmos Site MCP Server

Auf jeder Kundenwebsite soll ein eigener MCP-Endpunkt registriert werden, beispielsweise konzeptionell:

```text
/wp-json/kosmos-mcp/site
```

Nicht der konkrete Pfad, sondern eine saubere und konfliktfreie Implementierung ist entscheidend.

Dieser MCP-Server soll bevorzugt mit dem offiziellen WordPress MCP Adapter erstellt werden.

Der Server soll zunächst nur wenige Meta-Tools anbieten:

```text
discover abilities
get ability info
execute ability
```

Dadurch muss nicht jede WordPress Ability als eigenes Tool an den zentralen Kosmos Server weitergereicht werden.

Der zentrale Server kann dynamisch feststellen:

```text
Welche Abilities gibt es auf dieser Website?
Was macht die Ability?
Welche Parameter benötigt sie?
Darf sie ausgeführt werden?
```

---

# 6. Automatische Registrierung beim Kosmos Server

Wenn das Kosmos Bridge Plugin aktiviert wird, soll es sich automatisch beim zentralen Kosmos Server registrieren.

Dabei sollen mindestens übertragen werden:

```text
site_uuid
home_url
site_url
WordPress-Version
PHP-Version
Bridge-Version
MCP-Endpunkt
Zeitpunkt der Registrierung
```

Der zentrale Kosmos Server legt die Website zunächst als:

```text
pending
```

oder – wenn die Domain bereits eindeutig bekannt ist – nach erfolgreicher Prüfung als:

```text
verified
```

an.

Wenn eine Domain bereits einem Kunden zugeordnet ist, soll diese Zuordnung automatisch vorgeschlagen oder hergestellt werden können.

Beispiel:

```text
Domain:
bausachverstaendiger-erding.de

→ bekannte Website

→ Kunde:
Bausachverständiger Wolter
```

---

# 7. Authentifizierung

Es darf KEIN gemeinsames Master-Passwort in allen WordPress-Plugins hinterlegt werden.

Jede Website benötigt eigene Zugangsdaten bzw. eine eigene kryptografische Identität.

MVP-Lösung:

Bei der Aktivierung erzeugt das Bridge Plugin:

```text
site_uuid
site_secret
```

Das Secret muss kryptografisch sicher zufällig erzeugt werden.

Es darf nur für diese eine Website gültig sein.

Die Registrierung wird über HTTPS an den Kosmos Server übertragen.

Der Kosmos Server speichert das Secret verschlüsselt.

Anschließend sollen MCP-Anfragen zwischen Kosmos Server und WordPress nicht lediglich mit einem statischen Header-Key erfolgen, sondern möglichst per HMAC signiert werden.

Beispielsweise unter Einbeziehung von:

```text
site_uuid
timestamp
nonce
request body hash
signature
```

Dadurch sollen Replay-Angriffe erschwert werden.

Der WordPress MCP Adapter unterstützt eigene Transport-Permission-Callbacks. Diese Möglichkeit soll verwendet werden.

Später kann gegebenenfalls auf Public-/Private-Key-Signaturen umgestellt werden.

---

# 8. Sicherheitsregeln

Das System erhält später weitreichende Rechte auf Kundenwebsites.

Sicherheit hat deshalb hohe Priorität.

Folgende Regeln gelten:

- kein globales Master-Secret für alle Websites
- pro Website eigene Credentials
- Secrets niemals im Git-Repository
- Secrets im zentralen System verschlüsselt speichern
- HTTPS zwingend
- Timestamp + Nonce gegen Replay-Angriffe
- Capability- und Permission-Prüfungen in WordPress nicht umgehen
- Eingaben strikt validieren
- Ausgaben normalisieren
- alle schreibenden Aktionen protokollieren
- keine Möglichkeit zur beliebigen PHP-Codeausführung
- kein Shell-Execute-Tool
- kein beliebiges SQL-Tool
- keine beliebigen Dateisystem-Schreiboperationen
- Destructive Tools klar kennzeichnen
- möglichst Least Privilege
- Fehler dürfen keine Secrets offenlegen

Jede Aktion soll nachvollziehbar sein.

---

# 9. WordPress Abilities

Die WordPress Abilities API soll als standardisierte Funktionsschicht verwendet werden.

Vorhandene Abilities von:

```text
WordPress Core
Plugins
Elementor-Erweiterungen
Kosmos Bridge
```

sollen zentral entdeckt werden können.

Der Kosmos MCP Server soll daher nicht fest davon ausgehen, dass jede Website dieselben Abilities besitzt.

---

# 10. Capability-Katalog im Kosmos Server

Der Kosmos Server soll zwischen einer abstrakten Fähigkeit und der konkreten WordPress Ability unterscheiden.

Beispiel:

```text
Abstrakte Fähigkeit:
elementor.element.update
```

Auf Website A könnte dafür verfügbar sein:

```text
elementor-tools/update-element-settings
```

Auf Website B später vielleicht:

```text
kosmos-elementor/update-element
```

Der Kosmos Server soll deshalb einen Capability-Katalog besitzen.

Beispiele:

```text
wordpress.site.info
wordpress.plugins.list
wordpress.plugins.updates
wordpress.plugin.update
wordpress.core.update

elementor.page.read
elementor.element.read
elementor.element.update
elementor.widget.insert

maintenance.backup
maintenance.healthcheck
maintenance.frontendcheck
```

Die tatsächliche Ability kann je Website unterschiedlich sein.

---

# 11. Elementor und vorhandene Abilities

Keine einzelne Elementor-Erweiterung oder kein einzelnes Drittanbieter-Plugin
soll eine architektonische Sonderrolle im Kosmos System erhalten.

Wenn auf einer Website bereits geeignete Elementor-Abilities vorhanden sind,
sollen diese bevorzugt verwendet werden.

Das System soll zunächst prüfen:

```text
Welche Elementor-Abilities sind auf der Website registriert?
```

Wenn benötigte Elementor-Funktionalität bereits vorhanden ist:

```text
→ vorhandene Ability benutzen
```

Wenn sie nicht vorhanden ist:

```text
→ Capability als unsupported melden
```

Erst später kann entschieden werden, ob die fehlende Fähigkeit als eigene Kosmos-Elementor-Ability entwickelt wird.

NICHT im ersten Schritt die komplette Elementor-Manipulation selbst nachbauen.

---

# 12. Mehrere MCP-Provider pro Website

Nicht fest davon ausgehen, dass alle vorhandenen WordPress- oder Plugin-Abilities
automatisch ueber einen einzigen Site-MCP-Endpunkt erreichbar sind.

Beim Entwickeln prüfen:

1. Welche Abilities registrieren WordPress und die installierten Plugins?
2. Welche davon sind für MCP öffentlich?
3. Können sie über die Meta-Tools des WordPress MCP Adapters entdeckt und ausgeführt werden?
4. Falls nicht, unterstützt der Kosmos Server zusätzlich mehrere MCP-Endpunkte pro Website.

Beispiel:

```text
Website Müller

MCP connections:
- Kosmos Site MCP
- weiterer WordPress MCP Provider
```

Die Architektur muss mehrere Provider pro Website unterstützen können.

---

# 13. Eigene Kosmos Abilities

Eigene Abilities nur ergänzen, wenn entsprechende Funktionen nicht bereits sinnvoll vorhanden sind.

Voraussichtlich werden beispielsweise folgende Funktionen benötigt:

```text
kosmos/get-plugin-inventory
kosmos/get-update-status

kosmos/update-plugin
kosmos/update-theme
kosmos/update-wordpress

kosmos/get-debug-information
kosmos/get-recent-errors
```

Vor Implementierung jeder Ability prüfen:

```text
Existiert bereits eine geeignete Ability?
```

Wenn ja:

```text
vorhandene verwenden
```

Wenn nein:

```text
Kosmos Ability implementieren
```

Neue Funktionen sollen dabei standardmaessig zuerst als `hub-first`
gedacht werden:

- neue Such-, Mapping-, Inventar- und Workflow-Logik gehoert primaer in den
  zentralen Kosmos Server
- vorhandene WordPress- und Plugin-Abilities sollen wiederverwendet werden
- `Kosmos Bridge` soll moeglichst klein und stabil bleiben

Ein neues `Kosmos Bridge`-Release ist primaer nur dann sinnvoll, wenn die
Aenderung wirklich lokal auf der Website passieren muss, zum Beispiel bei:

- neuen eigenen Site-Abilities im Plugin
- Aenderungen an Registrierung, Authentifizierung oder Transport
- neuen lokalen Endpunkten oder Sicherheitsmechanismen
- Fehlern direkt im Plugin selbst

---

# 14. Updates

Plugin-, Theme- und WordPress-Core-Updates sollen über offizielle WordPress-Funktionen ausgeführt werden.

Keine direkten Dateimanipulationen.

Beispielablauf:

```text
Codex:
"Update Elementor bei Kunde Müller."

↓
Kosmos MCP:
Kunde Müller suchen

↓
Site Registry:
mueller.de bestimmen

↓
Site MCP:
Abilities feststellen

↓
Capability:
wordpress.plugin.update

↓
konkrete Ability ausführen

↓
Ergebnis zurückgeben

↓
Wartungshistorie schreiben
```

---

# 15. Datenbank des Kosmos Servers

Es soll eine relationale Datenbank verwendet werden.

Produktiv bevorzugt PostgreSQL.

Mindestens folgende Entitäten vorsehen.

## customers

```text
id
external_id
name
zoho_id
created_at
updated_at
```

## sites

```text
id
uuid
customer_id
domain
home_url
site_url
status
wordpress_version
php_version
bridge_version
last_seen_at
registered_at
verified_at
created_at
updated_at
```

## site_connections

Damit mehrere MCP-/System-Verbindungen pro Website möglich sind:

```text
id
site_id
provider
endpoint
auth_type
encrypted_credentials
status
last_success_at
last_error_at
created_at
updated_at
```

Beispiel Provider:

```text
kosmos-wordpress
other-wordpress-mcp
```

## site_capabilities

```text
id
site_id
capability
provider
ability_name
ability_schema
read_only
destructive
last_discovered_at
```

## maintenance_runs

```text
id
site_id
started_at
completed_at
status
initiated_by
summary
```

## maintenance_actions

```text
id
maintenance_run_id
site_id
action
ability_name
input_summary
result_summary
status
started_at
completed_at
```

## site_snapshots

Für bekannte technische Zustände:

```text
id
site_id
captured_at
wordpress_version
php_version
plugins_json
themes_json
environment_json
```

## audit_log

```text
id
site_id
actor
source
action
result
timestamp
request_id
```

---

# 16. Historische Daten statt unnötiger Live-Abfragen

Der Kosmos Server soll ein eigenes Gedächtnis besitzen.

Beispiel:

```text
Wann wurde Kunde Müller zuletzt gewartet?
```

Diese Information soll aus der Kosmos-Datenbank kommen.

Nicht jedes Mal die Kundenwebsite abfragen.

Dagegen:

```text
Welche Elementor-Version läuft JETZT auf Müller?
```

kann eine Live-Abfrage auslösen.

Danach soll der aktuelle Stand wieder gespeichert werden.

Grundregel:

```text
Historische Information
→ Datenbank

Aktueller technischer Zustand
→ bei Bedarf Live-Abfrage

Live-Ergebnis
→ Datenbank aktualisieren
```

---

# 17. Kosmos MCP Tools für Codex

Der zentrale MCP Server soll zunächst bewusst wenige, allgemeine Tools anbieten.

Nicht hunderte Tools erzeugen.

MVP ungefähr:

```text
search_customers
get_customer

search_sites
get_site

discover_site_capabilities
get_site_capability

execute_site_capability

get_site_history
get_maintenance_history

refresh_site_inventory
```

Wichtig ist insbesondere:

```text
execute_site_capability(
    site_id,
    capability_or_ability,
    parameters
)
```

Der Kosmos Server übernimmt:

```text
Site bestimmen
↓
richtigen MCP Provider bestimmen
↓
MCP Session aufbauen
↓
Tool/Ability ausführen
↓
Antwort normalisieren
↓
Audit Log schreiben
↓
Ergebnis an Codex zurückgeben
```

Später können häufig genutzte Funktionen als komfortable High-Level-Tools ergänzt werden:

```text
update_plugin
run_maintenance
check_site
backup_site
```

---

# 18. MCP Proxy / Gateway

Der Kosmos Server ist nicht nur MCP Server.

Er ist gleichzeitig MCP Client.

Er benötigt daher:

```text
MCP Server:
Codex → Kosmos

MCP Client:
Kosmos → WordPress Websites
```

Für die WordPress-Verbindungen soll Streamable HTTP verwendet werden, sofern vom jeweiligen MCP-Endpunkt unterstützt.

Sessions müssen sauber:

```text
initialisiert
verwendet
beendet
```

werden.

Timeouts, Retry-Verhalten und Fehlerbehandlung implementieren.

Keine endlosen Retries.

---

# 19. Website Registrierung und Heartbeat

Die Bridge soll regelmäßig einen sehr kleinen Heartbeat senden können.

Beispielsweise einmal täglich.

Enthalten:

```text
site_uuid
Bridge-Version
WordPress-Version
PHP-Version
timestamp
```

Dadurch kann die Zentrale feststellen:

```text
online
unknown
possibly unavailable
```

Nicht unnötig häufig pollen.

---

# 20. Zentrale Web-App

Der Kosmos Server soll nicht ausschließlich MCP Server sein.

Er soll gleichzeitig Backend für eine kleine Verwaltungsoberfläche sein.

Die Geschäftslogik muss unabhängig von MCP implementiert werden.

Architektur:

```text
                 Business Logic
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
         MCP          REST        Web UI
        Codex       andere Apps   Mitarbeiter
```

Keine Geschäftslogik ausschließlich in MCP-Handler schreiben.

---

# 21. Erste Weboberfläche

MVP-Weboberfläche:

## Dashboard

```text
Anzahl Websites
online
offline/unbekannt
Wartung fällig
Registrierung ausstehend
Fehler
```

## Websites

Tabelle:

```text
Kunde
Domain
Status
WordPress
PHP
Bridge
letzter Kontakt
letzte Wartung
```

## Website Detail

Anzeigen:

```text
Kunde
Domain
Verbindungen
bekannte Abilities
Plugins
WordPress-Version
PHP-Version
letzte Wartungen
Audit Log
```

## Neue Registrierungen

```text
pending Websites
bekannte Domain zuordnen
unbekannte Domain freigeben/ablehnen
```

---

# 22. Empfohlener Technologie-Stack

## Zentraler Kosmos Server

Bevorzugt:

```text
Python 3.12+
FastAPI
PostgreSQL
SQLAlchemy
Alembic
Pydantic
```

Für MCP:

Aktuelle offizielle bzw. etablierte MCP-Python-Bibliothek verwenden.

Keine eigene MCP-Protokollimplementierung schreiben, wenn eine geeignete gepflegte Bibliothek existiert.

## WordPress Plugin

```text
PHP
WordPress 6.9+
Abilities API
wordpress/mcp-adapter
Composer
```

Aus Kompatibilitätsgründen möglichst keine unnötig hohe PHP-Mindestversion voraussetzen.

Der offizielle MCP Adapter unterstützt PHP 7.4+, daher beim Bridge-MVP möglichst kompatibel bleiben, sofern keine technische Notwendigkeit für eine höhere Version besteht.

---

# 23. Repository-Struktur

Beispielsweise:

```text
kosmos-platform/
│
├── server/
│   ├── app/
│   │   ├── api/
│   │   ├── mcp/
│   │   ├── models/
│   │   ├── services/
│   │   ├── repositories/
│   │   ├── security/
│   │   └── wordpress/
│   │
│   ├── migrations/
│   └── tests/
│
├── wordpress-plugin/
│   ├── kosmos-bridge.php
│   ├── src/
│   │   ├── Abilities/
│   │   ├── Mcp/
│   │   ├── Registration/
│   │   ├── Security/
│   │   └── Health/
│   ├── vendor/
│   └── composer.json
│
└── docs/
    ├── architecture.md
    ├── security.md
    ├── protocol.md
    └── development.md
```

Die genaue Struktur darf verbessert werden, wenn eine sauberere Lösung sinnvoll ist.

---

# 24. Entwicklung in Phasen

Nicht alles gleichzeitig implementieren.

## Phase 1 – Fundament

Ziel:

```text
Kosmos Server läuft
Datenbank läuft
Bridge Plugin läuft
Website kann sich registrieren
Website erscheint zentral
```

Noch KEINE Updates durchführen.

Noch KEINE Elementor-Schreiboperationen.

Implementieren:

- Datenmodell
- FastAPI Backend
- Bridge Grundgerüst
- automatische Registrierung
- Site UUID
- Site Secret
- sichere Authentifizierung
- Statusprüfung
- minimale Weboberfläche
- Audit Logging

---

## Phase 2 – MCP Verbindung

Ziel:

```text
Codex
↓
Kosmos MCP
↓
WordPress MCP
↓
Ability
```

Implementieren:

- Kosmos MCP Server
- MCP Client für WordPress
- Kosmos Site MCP Server im Bridge Plugin
- Ability Discovery
- Ability Schema abrufen
- Ability ausführen
- Fehler sauber zurückgeben

Zunächst nur READ-ONLY Abilities verwenden.

Beispiel:

```text
core/get-site-info
core/get-environment-info
```

Akzeptanztest:

Codex kann fragen:

```text
"Welche WordPress-Version läuft auf Website X?"
```

und erhält die Information über:

```text
Codex
→ Kosmos MCP
→ Kundenwebsite MCP
→ WordPress Ability
```

---

## Phase 3 – Capability Discovery

Alle verfügbaren Abilities einer Website erfassen.

Capability-Mapping implementieren.

Beispiel:

```text
elementor-tools/get-page-structure

→

elementor.page.read
```

Abilities regelmäßig oder auf Anforderung aktualisieren.

---

## Phase 4 – WordPress Updates

Erst jetzt schreibende Abilities ergänzen.

Benötigt:

```text
Plugins auflisten
Updates feststellen
ein Plugin aktualisieren
Theme aktualisieren
WordPress aktualisieren
```

Vorhandene Abilities zuerst prüfen.

Nur fehlende als Kosmos Ability implementieren.

Alle Aktionen auditieren.

---

## Phase 5 – Elementor

Vorhandene Elementor- und andere WordPress-Abilities untersuchen.

Wenn vorhanden:

```text
verwenden
```

Wenn nicht vorhanden:

```text
fehlende Fähigkeiten dokumentieren
```

Nur gezielt fehlende Elementor-Abilities selbst implementieren.

Kein kompletter Elementor-Klon.

---

## Phase 6 – Wartungsabläufe

Nun kann Codex mehrere Tools kombinieren.

Beispiel:

```text
Website auswählen
↓
Updates feststellen
↓
Backup über geeignetes Tool/Mittwald
↓
Update durchführen
↓
Website prüfen
↓
Ergebnis dokumentieren
↓
Zoho Notiz erstellen
```

Der Ablauf soll nicht zwingend als starrer Workflow programmiert werden.

Codex soll einzelne Fähigkeiten flexibel kombinieren können.

Für sicherheitskritische Schritte dürfen jedoch feste Regeln/Gates existieren.

---

# 25. Sicherheits-Gates für schreibende Aktionen

Folgende Regeln vorbereiten:

## Read-only

Automatisch erlaubt:

```text
Site Info
Plugin-Liste
Versionen
Ability Discovery
Statusinformationen
```

## Write

Kennzeichnen:

```text
Plugin Update
WP Update
Elementor Änderung
Settings ändern
```

## High Risk

Besonders schützen:

```text
Plugin löschen
Theme löschen
Rollback
Dateien verändern
Benutzer verändern
```

Im MVP keine Werkzeuge für beliebige Codeausführung anbieten.

---

# 26. Fehlerbehandlung

Jeder Remote-Aufruf benötigt eindeutige Fehlerarten.

Beispiele:

```text
SITE_OFFLINE
AUTH_FAILED
MCP_NOT_AVAILABLE
ABILITY_NOT_FOUND
ABILITY_NOT_ALLOWED
VALIDATION_FAILED
REMOTE_TIMEOUT
WORDPRESS_ERROR
PLUGIN_UPDATE_FAILED
```

Codex soll keine unstrukturierten PHP-Fehlerseiten erhalten.

Der zentrale Server normalisiert die Fehler.

---

# 27. Logging und Audit

Jede relevante Aktion erhält:

```text
request_id
site_id
actor
source
action
timestamp
status
duration
result summary
```

Nicht loggen:

```text
Passwörter
Tokens
Secrets
vollständige Authorization Header
```

---

# 28. Wichtige Architekturregeln

Diese Regeln während der gesamten Entwicklung beachten:

1. MCP nicht selbst neu erfinden.
2. Offiziellen WordPress MCP Adapter verwenden.
3. WordPress Abilities verwenden.
4. Vorhandene Abilities wiederverwenden.
5. Keine einzelne Drittanbieter-Erweiterung als Sonderfall voraussetzen.
6. Kosmos Bridge klein halten.
7. Keine 200 MCP-Verbindungen in Codex konfigurieren.
8. Kosmos Server als zentrales MCP Gateway verwenden.
9. Geschäftslogik nicht an MCP koppeln.
10. Historische Daten zentral speichern.
11. Live-Abfragen nur bei Bedarf.
12. Pro Website eigene Authentifizierung.
13. Keine globalen Website-Secrets.
14. Schreibende Aktionen vollständig auditieren.
15. Keine beliebige Code-/Shell-Ausführung anbieten.
16. Architektur modular halten.

---

# 29. Vorgehen für Codex

Arbeite nicht sofort an allen Funktionen.

Beginne mit einer Analyse und erstelle zunächst:

```text
docs/architecture.md
docs/security.md
docs/development-plan.md
```

Prüfe dabei den aktuellen Stand der offiziellen APIs und Bibliotheken:

- WordPress Abilities API
- WordPress MCP Adapter
- MCP Streamable HTTP
- aktuell geeignete Python MCP Bibliothek
- vorhandene WordPress- und Plugin-Abilities, soweit relevant

Verlasse dich bei aktuellen APIs nicht auf veraltetes Wissen.

Danach Phase 1 implementieren.

Nach jeder Phase:

1. Code überprüfen
2. unnötige Komplexität entfernen
3. Sicherheitsprobleme suchen
4. Architektur-Dokumentation aktualisieren
5. Akzeptanzkriterien prüfen

Keine Platzhalterarchitektur bauen, die später vollständig ersetzt werden muss.

---

# 30. Erstes konkretes Entwicklungsziel

Der erste vollständige End-to-End-Test soll möglichst klein sein:

```text
1. Kosmos Bridge auf Test-WordPress installieren.

2. Plugin aktiviert sich.

3. Plugin erzeugt Site UUID und Credentials.

4. Plugin registriert sich beim Kosmos Server.

5. Kosmos Server speichert die Website.

6. Kosmos Server kann sich authentifiziert mit dem
   Site-MCP verbinden.

7. Kosmos Server entdeckt:
   core/get-site-info

8. Codex verbindet sich ausschließlich mit Kosmos MCP.

9. Benutzer sagt:

   "Zeige mir die WordPress-Version der Testwebsite."

10. Codex ruft Kosmos MCP auf.

11. Kosmos MCP ermittelt die Website.

12. Kosmos Server ruft den WordPress MCP auf.

13. WordPress führt die Ability aus.

14. Antwort läuft zurück:

WordPress
→ Kosmos
→ Codex
→ Benutzer
```

Wenn dieser Ablauf stabil und sicher funktioniert, erst dann weitere Abilities und Funktionen ergänzen.

---

# 31. Langfristiges Zielbild

Das fertige System soll letztlich folgende Rolle erfüllen:

```text
                         CODEX
                           │
                 natürliche Sprache
                           │
          ┌────────────────┼─────────────────┐
          │                │                 │
          ▼                ▼                 ▼
      KOSMOS MCP      MITTWALD MCP       ZOHO MCP
          │
          ▼
   Kosmos Plattform
          │
    ┌─────┼─────────────────────┐
    │     │                     │
    ▼     ▼                     ▼
 Kunde A Kunde B             Kunde N
    │
 WordPress MCP
    │
 Abilities
    │
 ┌──┴──────────────────────────┐
 │                             │
WordPress                  Elementor
 │                             │
 └──────── verfügbare Tools ───┘
```

Der Kosmos Server bildet dabei das zentrale Wissen über:

```text
Kunden
Websites
Verbindungen
Capabilities
Wartungen
Historie
technische Zustände
```

Codex bildet die intelligente Orchestrierung.

WordPress/Mittwald/Zoho und andere Systeme stellen die eigentlichen Werkzeuge bereit.

**Funktionen sollen dort ausgeführt werden, wo sie fachlich hingehören. Der Kosmos MCP soll sie zentral auffindbar und für Codex nutzbar machen, nicht unnötig nachbauen.**
