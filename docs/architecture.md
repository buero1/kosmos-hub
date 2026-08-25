# Kosmos Hub Architecture

Stand: 2026-08-25

## Ziel

`kosmos-hub` ist die zentrale Verwaltungs- und Gateway-Schicht fuer viele
WordPress-Kundenwebsites. Codex soll spaeter primaer nur mit `kosmos-hub` als
MCP-Server sprechen. `kosmos-hub` kennt Kunden, Websites, Verbindungen,
Capability-Mappings, Historie und Audit-Daten.

## Gepruefte Grundlagen

Die Phase-1-Architektur basiert auf aktuell verifizierten Primaerquellen:

- WordPress Abilities API ist Teil von WordPress `6.9+` und beschreibt
  discoverable abilities mit JSON-Schema und Permission-Callbacks:
  <https://developer.wordpress.org/apis/abilities-api/>
- Der offizielle WordPress MCP Adapter exponiert Abilities als MCP-Tools,
  unterstuetzt HTTP und STDIO und bietet Meta-Tools fuer Discovery und
  Execution:
  <https://github.com/WordPress/mcp-adapter/blob/trunk/README.md>
- Das offizielle Python MCP SDK ist aktuell die stabile `v2`-Linie und
  unterstuetzt die MCP-Spezifikation `2026-07-28`:
  <https://github.com/modelcontextprotocol/python-sdk>
- Streamable HTTP nutzt in der aktuellen Spezifikation einen einzelnen
  HTTP-POST-Endpunkt pro MCP-Server und verlangt unter anderem Origin-Pruefung:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http>

## Architekturprinzipien

1. Bestehende Faehigkeiten dort nutzen, wo sie fachlich hingehoeren.
2. WordPress-Funktionalitaet nicht unnnoetig in `kosmos-hub` nachbauen.
3. Pro Website eine eigene Identitaet und eigene Credentials.
4. Geschaeftslogik nicht an MCP koppeln.
5. Historie zentral speichern, Live-Abfragen nur bei Bedarf.
6. Schreibende Aktionen erst nach sauberem Audit und Sicherheits-Gates.

## Erweiterungsstrategie

Neue fachliche Faehigkeiten sollen kuenftig standardmaessig zuerst als
`hub-first` gedacht werden. Das bedeutet:

- Neue Orchestrierung, Auswertung, Suche, Mapping-Logik und Inventarlogik
  gehoeren primaer in `kosmos-hub`.
- Vorhandene WordPress-Abilities und andere bestehende Provider sollen
  bevorzugt wiederverwendet werden.
- `Kosmos Bridge` bleibt moeglichst klein und stabil, weil sie spaeter auf
  vielen Kundenwebsites ausgerollt ist.

Ein neues Bridge-Release ist nur dann der bevorzugte Weg, wenn die Aenderung
wirklich lokal auf der Website passieren muss, zum Beispiel bei:

- neuen eigenen Site-Abilities im Plugin
- Aenderungen an Registrierung, Authentifizierung oder Transport
- neuen lokalen Endpunkten oder Sicherheitsmechanismen
- Fehlern, die direkt im Plugin selbst liegen

Kein Bridge-Release sollte noetig sein fuer:

- neue Hub-Tools oder neue Hub-Workflows
- neues Capability-Mapping
- neue Nutzung bereits vorhandener WordPress-Abilities
- neue Kombinationen aus Hub, WordPress und spaeter weiteren Providern

## Monorepo-Entscheidung

Server, Plugin und Dokumentation liegen zunaechst bewusst in einem Monorepo.
Das ist fuer die ersten Phasen sinnvoll, weil:

- Protokoll, Sicherheitsmodell und Datenmodell eng zusammenhaengen
- die End-to-End-Integration zwischen Server und Plugin der Haupttreiber ist
- wir spaeter immer noch in getrennte Repos extrahieren koennen

## Systemkontext

```text
Codex
  |
  | MCP
  v
kosmos-hub
  | \
  |  \-- spaeter: Mittwald MCP, Zoho MCP, weitere Provider
  |
  +-- zentrale Datenbank
  |
  +-- HTTP-Registrierung / Heartbeat
  |
  +-- spaeter: MCP-Client zu Site-MCP-Endpunkten
        |
        v
   WordPress Site + Kosmos Bridge
```

## Phase-1-Scope

Phase 1 bleibt absichtlich klein:

- FastAPI-Backend fuer Site-Registry
- relationale Datenhaltung mit SQLAlchemy, zunaechst auf MariaDB
- Registrierungs-Endpoint fuer `Kosmos Bridge`
- verschluesselte Speicherung des pro-Site-Secrets
- einfacher Heartbeat
- minimale Weboberflaeche fuer Dashboard und Site-Liste
- Audit-Logging fuer Registrierung und Heartbeat

Noch nicht Bestandteil von Phase 1:

- zentraler Kosmos MCP Server
- WordPress Site MCP Proxying
- Capability Discovery
- Plugin-/Core-Updates
- Elementor-Schreiboperationen
- Mittwald-/Zoho-Live-Integrationen

## Server-Architektur

### Stack

- Python 3.12+
- FastAPI
- SQLAlchemy 2.x
- Alembic
- Pydantic / pydantic-settings
- MariaDB via SQLAlchemy-Dialekt
- Jinja2 fuer die einfache MVP-Weboberflaeche

### Schichten

- `app/api/`: HTTP-API fuer Registrierung, Sites und Health
- `app/core/`: Settings und Security-Helfer
- `app/db/`: SQLAlchemy-Engine, Sessions und Model-Importe
- `app/models/`: Datenbankmodelle
- `app/repositories/`: Datenzugriff
- `app/services/`: Registrierungs-, Verschluesselungs- und Audit-Logik
- `app/templates/`: einfache serverseitige HTML-Ausgabe

Die Hub-Weboberflaeche nutzt ein datenbankgestuetztes Benutzerkonto statt einer
statischen Server-Basic-Auth. Der erste Administrator wird ueber einen nur auf
dem Host erzeugbaren Einmal-Link eingerichtet. Danach schuetzen signierte
HTTPS-Sitzungen die Hub-Seiten und internen APIs; die HMAC-Registrierung der
WordPress-Sites bleibt davon bewusst getrennt.

## Datenmodell in Phase 1

Phase 1 implementiert die Tabellen, die fuer Registrierung und Sichtbarkeit
sofort gebraucht werden:

- `customers`
- `sites`
- `site_connections`
- `audit_log`

Die spaeteren Entitaeten `site_capabilities`, `maintenance_runs`,
`maintenance_actions` und `site_snapshots` sind bereits im Plan vorgesehen,
aber noch nicht im Scaffold materialisiert.

Mit dem aktuellen Build ist `site_capabilities` als erste Phase-3-Entitaet
materialisiert. Zunaechst speichert sie die konkret entdeckten Ability-Namen
und Schemas pro Provider. Das eigentliche abstrakte Capability-Mapping bleibt
bewusst ein nachgelagerter Schritt.

Ebenfalls materialisiert ist jetzt `site_snapshots` als erster technischer
Inventar-Baustein. Zunaechst speichert `kosmos-hub` darueber live abgefragte
WordPress-/PHP-Versionen, aktive Plugins und das zugehoerige
Umgebungsobjekt der Site.

Die gespeicherten Capabilities werden dabei bewusst provider-neutral behandelt.
`kosmos-hub` soll vorhandene WordPress- und Plugin-Abilities allgemein nutzen,
ohne einzelne Drittanbieter-Plugins architektonisch als Sonderfall in den
Vordergrund zu stellen.

## Registrierungsfluss

1. `Kosmos Bridge` wird auf einer WordPress-Site aktiviert.
2. Das Plugin erzeugt `site_uuid` und `site_secret`.
3. Das Plugin sendet Registrierungsdaten an `kosmos-hub`.
4. `kosmos-hub` legt oder aktualisiert `site` und `site_connection`.
5. Das Site-Secret wird serverseitig verschluesselt gespeichert.
6. Ein Audit-Eintrag dokumentiert die Registrierung.

## Authentifizierungsmodell in Phase 1

Phase 1 implementiert bereits die Form des spaeteren HMAC-Schemas:

- `X-Kosmos-Site-UUID`
- `X-Kosmos-Timestamp`
- `X-Kosmos-Nonce`
- `X-Kosmos-Body-SHA256`
- `X-Kosmos-Signature`

Bootstrap-Einschraenkung:

Beim allerersten Registrierungsaufruf kennt der Server das Secret noch nicht.
Darum kann Phase 1 die erste Registrierung nur ueber HTTPS und Payload-Konsistenz
absichern. Ab dem zweiten Request kann das gespeicherte Secret fuer echte
HMAC-Verifikation genutzt werden.

Diese Einschraenkung ist bewusst dokumentiert und spaeter verbesserbar, etwa
ueber asymmetrisches Onboarding oder pro-Site-Einmal-Tokens.

## WordPress-Plugin-Rolle in Phase 1

`Kosmos Bridge` ist in Phase 1 bewusst klein:

- Site-Identitaet erzeugen und speichern
- Registrierung an `kosmos-hub`
- taeglichen Heartbeat senden
- Admin-Statusseite fuer lokalen Installationsstatus

Die eigentliche Site-MCP-Exposition bleibt fuer Phase 2 vorgesehen. Der
Composer-Slot fuer `wordpress/mcp-adapter` ist bereits vorbereitet.

## Warum MariaDB zuerst

MariaDB laeuft bereits auf dem Zielserver. Fuer das Fundament bringt ein
PostgreSQL-Wechsel aktuell mehr Infrastrukturarbeit als fachlichen Nutzen.

Deshalb:

- Phase 1 startet mit MariaDB
- SQLAlchemy trennt die Anwendung weitgehend vom konkreten Datenbankdialekt
- ein spaeterer Umstieg auf PostgreSQL bleibt moeglich
