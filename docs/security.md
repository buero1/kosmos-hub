# Kosmos Hub Security

Stand: 2026-08-25

## Sicherheitsziele

Das System soll spaeter schreibend auf viele Kundenwebsites zugreifen koennen.
Deshalb gilt schon im MVP:

- keine globalen Website-Secrets
- pro Website eigene Identitaet
- Secrets nie im Repository
- schreibende Aktionen auditiert
- keine beliebige Code-, Shell- oder SQL-Ausfuehrung

## Aktuell verifizierte Protokoll- und Plattform-Annahmen

- Die WordPress Abilities API nutzt JSON Schema und Permission-Callbacks:
  <https://developer.wordpress.org/apis/abilities-api/>
- Der WordPress MCP Adapter unterstuetzt pro-Transport-Authentifizierung und
  per-Ability-Permission-Pruefung:
  <https://github.com/WordPress/mcp-adapter/blob/trunk/README.md>
- Die aktuelle MCP-Streamable-HTTP-Spezifikation verlangt Origin-Validierung
  und ordentliche Authentifizierung:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http>

## Phase-1-Bedrohungsbild

Die ersten realen Risiken sind:

- abgefangene oder falsch geleitete Registrierungsdaten
- unsaubere Secret-Speicherung auf der Zentrale
- fehlende Nachvollziehbarkeit von Aktionen
- Replay- oder Spoofing-Versuche bei wiederholten Requests
- versehentliche Ueberprivilegierung der MVP-Weboberflaeche

## Phase-1-Massnahmen

### 1. Per-Site-Credentials

Jede WordPress-Site erzeugt bei Aktivierung:

- `site_uuid`
- `site_secret`

Es gibt kein globales Master-Secret fuer alle Sites.

### 2. Secret-Speicherung

`site_secret` wird im zentralen Backend verschluesselt gespeichert. Das
Scaffold nutzt dafuer symmetrische Verschluesselung auf Basis eines separaten
Server-Secrets (`APP_SECRET_KEY`), das nur ueber Umgebungsvariablen gesetzt
werden darf.

### 3. Signierte Requests

Das Plugin sendet Registrierung und Heartbeat mit folgenden Headern:

- `X-Kosmos-Site-UUID`
- `X-Kosmos-Timestamp`
- `X-Kosmos-Nonce`
- `X-Kosmos-Body-SHA256`
- `X-Kosmos-Signature`

Die HMAC-Basis ist:

```text
site_uuid.timestamp.nonce.body_sha256
```

### 4. Bootstrap-Grenze der Erstregistrierung

Die erste Registrierung ist ein Sonderfall, weil der Server das Secret noch
nicht kennt. Das MVP loest das so:

- Registrierung nur ueber HTTPS
- Bootstrap-Request muss bereits mit dem im Payload enthaltenen Secret
  signiert sein
- Body-Hash wird geprueft
- `site_uuid` in Header und Payload muessen zusammenpassen
- Nonce und Timestamp werden auch serverseitig gegen Replay geprueft
- danach wird das Secret verschluesselt gespeichert
- alle Folge-Requests muessen gegen das gespeicherte Secret validierbar sein
- nach erfolgreichem Onboarding wird das Secret nicht mehr bei jedem normalen
  Heartbeat erneut uebertragen

Diese Stelle ist die wichtigste bekannte Sicherheitsgrenze des MVP und wird in
Phase 2/3 weiter gehaertet.

### 5. Audit Log

Jede relevante Aktion erzeugt einen Audit-Eintrag mit:

- `request_id`
- `site_id`
- `actor`
- `source`
- `action`
- `status`
- `result`
- `timestamp`

### 6. Admin-Loopback fuer Update-Inventur

Einige WordPress-Plugins stellen ihre Update-Angebote nur bereit, wenn der
Admin-Kontext geladen wurde. Die Bridge kann deshalb nach einem bereits
HMAC-autorisierten Hub-Aufruf eine Anfrage an die eigene `admin-ajax.php`
ausfuehren. Diese Anfrage ist zusaetzlich mit einem einmaligen, zufaelligen
Token abgesichert, der nur 60 Sekunden gueltig ist, als Hash im WordPress-
Transient gespeichert und vor der Auswertung verbraucht wird.

Der Loopback liest ausschliesslich Update-Metadaten. Wenn eine Site lokale
Loopback-Anfragen blockiert, faellt die Bridge kontrolliert auf die bisherige
rein lesende Update-Pruefung zurueck.

Nicht geloggt werden:

- Secrets
- vollstaendige Authorization-Header
- Passwoerter

### 7. Hub-Benutzerkonten und Sitzungen

Die Weboberflaeche und internen APIs sind durch ein eigenes Hub-Konto geschuetzt.
Ausgenommen bleiben ausschliesslich der Health-Endpoint und die HMAC-gesicherte
Site-Registrierung. Passwoerter werden mit `PBKDF2-HMAC-SHA256`, einem eigenen
zufaelligen Salt und 600.000 Iterationen gespeichert; Klartextpasswoerter werden
nicht persistiert oder geloggt.

Der erste Administrator wird nicht ueber eine oeffentliche Registrierung
angelegt. Der Server erzeugt dafuer auf einem direkten lokalen Aufruf einen
zufaelligen, einmalig nutzbaren Einrichtungslink, der nach 20 Minuten ablaeuft.
Der Token liegt nur als HMAC-Hash in der Datenbank und wird im Link-Fragment
transportiert, damit er nicht in Webserver-URLs landet.

Sitzungen sind signierte, nur ueber HTTPS uebertragene Cookies mit `SameSite=Lax`
und einer maximalen Laufzeit von 12 Stunden. Jede Sitzung enthaelt eine
Versionsnummer. Ein Passwortwechsel erhoeht diese Nummer und macht damit andere
bestehende Sitzungen ungueltig. Formulare mit schreibender Wirkung sind zusaetzlich
mit serverseitig gespeicherten CSRF-Tokens geschuetzt.

## Sicherheitsregeln fuer spaetere MCP-Endpunkte

Fuer Phase 2+ sind bereits jetzt verbindlich:

1. Origin-Pruefung auf Streamable-HTTP-Endpunkten
2. keine beliebige Tool-Exposition ohne `public`-Opt-in
3. Transport-Authentifizierung getrennt von WordPress-Capabilities
4. Destructive Tools klar markieren
5. keine Shell-, PHP- oder Raw-SQL-Tools

## Offene Sicherheitsaufgaben nach Phase 1

- asymmetrisches oder tokenbasiertes Erst-Onboarding
- feinere Rollen und getrennte Benutzerverwaltung fuer die Weboberflaeche
- Capability-spezifische Write-Gates
- strukturierte Alarmierung bei wiederholten Auth-Fehlern
