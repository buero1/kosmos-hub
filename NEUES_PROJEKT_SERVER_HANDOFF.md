# Server-Handoff fuer ein separates Codex-Projekt

Dieses Dokument ist die technische Uebergabe fuer ein zweites Projekt auf
derselben Infrastruktur. Das neue Projekt darf die bestehende Telefonie-App
nicht veraendern oder neu starten.

## Bestehende Infrastruktur

| Rolle | Server | Bestehende Installation | Schutzregel |
| --- | --- | --- | --- |
| App-Server | `31.70.92.95` | Telefonie-App unter `/opt/kosmos-api`, extern aktuell ueber `https://31-70-92-95.sslip.io` | Nicht anfassen |
| Voice-Server | `217.154.254.129` | Asterisk, SIP, App-Local-Worker und Telefonie-Konfiguration | Fuer ein normales neues Projekt nicht verbinden |

Die bestehende Telefonie-App verwendet mindestens diese Dienste und Pfade:

- `kosmos-api.service`
- `kosmos-call-runner.service`
- `kosmos-call-recording-sync.service`
- auf dem Voice-Server: `asterisk.service` und `kosmos-app-local-worker.service`
- `/opt/kosmos-api`
- `/etc/asterisk`

Diese Namen und Verzeichnisse sind fuer das neue Projekt gesperrt.

## Zielbild fuer Projekt Zwei

Im Folgenden steht `projekt2` als Platzhalter. Vor dem ersten Setup muss ein
eindeutiger, nur aus Kleinbuchstaben, Ziffern und Bindestrichen bestehender
Projektname festgelegt werden, zum Beispiel `kundenportal`.

| Bereich | Vorgabe |
| --- | --- |
| Linux-Benutzer | `projekt2` |
| Programmcode | `/opt/projekt2/app` |
| Python-Virtualenv | `/opt/projekt2/venv` |
| Konfiguration und Geheimnisse | `/etc/projekt2/projekt2.env`, Modus `600`, Eigentumer `root:projekt2` |
| Laufzeitdaten | `/var/lib/projekt2` |
| Systemdienst | `projekt2-api.service` |
| Datenbank | eigene MariaDB-Datenbank `projekt2` und eigener MariaDB-Benutzer `projekt2_app` |
| Oeffentlicher Hostname | eigene Subdomain, z. B. `projekt2.deinedomain.de` |
| Interner Web-Port | eigener lokaler Port, z. B. `127.0.0.1:8102` |

Es darf keine gemeinsame `.env`, kein gemeinsamer Virtualenv, kein gemeinsamer
Systemdienst und keine gemeinsame Datenbank mit `kosmos-api` geben.

## Zugang fuer Codex

### Einmalig durch einen Server-Administrator

1. Einen separaten SSH-Schluessel fuer das neue Projekt erzeugen.
2. Einen eingeschraenkten Benutzer `projekt2deploy` auf `31.70.92.95` anlegen.
3. Nur dessen Public Key in `~projekt2deploy/.ssh/authorized_keys` hinterlegen.
4. Dem Benutzer gezielt Schreibrechte auf `/opt/projekt2/app` geben.
5. Per `sudoers` ausschliesslich diese Kommandos ohne Passwort erlauben:
   - `systemctl restart projekt2-api.service`
   - `systemctl status projekt2-api.service`
   - `journalctl -u projekt2-api.service`
6. Root-Zugang, Zugriff auf `/opt/kosmos-api`, `/etc/asterisk` und den
   Voice-Server nicht erteilen.

### Lokale Codex-Konfiguration

Der private Schluessel wird ausschliesslich lokal abgelegt und weder in Git noch
in den Projektdateien gespeichert. Beispiel unter Windows:

```powershell
$env:PROJEKT2_SERVER = "projekt2deploy@31.70.92.95"
$env:PROJEKT2_SSH_KEY = "$HOME\.ssh\projekt2_deploy_ed25519"

ssh -i $env:PROJEKT2_SSH_KEY $env:PROJEKT2_SERVER "whoami; hostname; pwd"
```

Erwartetes Ergebnis: Benutzer `projekt2deploy`, der App-Server und kein
Root-Shell-Zugang.

Codex im neuen Projekt erhaelt diesen Kontext:

```text
Deployment-Ziel: projekt2deploy@31.70.92.95
Privater SSH-Key: lokal unter %USERPROFILE%\.ssh\projekt2_deploy_ed25519
Projekt-Root auf dem Server: /opt/projekt2/app
Systemdienst: projekt2-api.service
Umgebung: /etc/projekt2/projekt2.env
Erlaubt: Code nach /opt/projekt2/app kopieren, projekt2-api neu starten,
Logs dieses Dienstes lesen.
Verboten: Root verwenden, /opt/kosmos-api, kosmos-*.service, /etc/asterisk
oder 217.154.254.129 veraendern oder neu starten.
```

## Ersteinrichtung auf dem Server

Die folgenden Befehle sind ein einmaliges Administrator-Setup. Sie werden nicht
als regulaerer Deployment-Schritt ausgefuehrt.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin projekt2
sudo useradd --create-home --shell /bin/bash projekt2deploy

sudo install -d -o projekt2 -g projekt2 -m 750 /opt/projekt2/app /var/lib/projekt2
sudo python3 -m venv /opt/projekt2/venv
sudo chown -R projekt2:projekt2 /opt/projekt2 /var/lib/projekt2

sudo install -d -o root -g projekt2 -m 750 /etc/projekt2
sudo install -o root -g projekt2 -m 640 /dev/null /etc/projekt2/projekt2.env
```

Die Datenbank wird separat erstellt. Werte mit echten Passwoertern gehoeren nur
in `/etc/projekt2/projekt2.env`:

```sql
CREATE DATABASE projekt2 CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'projekt2_app'@'localhost' IDENTIFIED BY 'EIN_EIGENES_STARKES_PASSWORT';
GRANT ALL PRIVILEGES ON projekt2.* TO 'projekt2_app'@'localhost';
FLUSH PRIVILEGES;
```

Beispiel fuer die Umgebungsdatei:

```dotenv
APP_ENV=production
DATABASE_URL=mysql://projekt2_app:EIN_EIGENES_STARKES_PASSWORT@127.0.0.1:3306/projekt2
SECRET_KEY=EIN_EIGENER_LANGER_ZUFAELLIGER_WERT
```

## Systemdienst

Der Dienst bekommt einen eigenen Namen und laeuft als `projekt2`, nicht als
Root. Beispiel fuer `/etc/systemd/system/projekt2-api.service`:

```ini
[Unit]
Description=Projekt Zwei API
After=network.target mariadb.service
Wants=mariadb.service

[Service]
User=projekt2
Group=projekt2
WorkingDirectory=/opt/projekt2/app
EnvironmentFile=/etc/projekt2/projekt2.env
Environment=PATH=/opt/projekt2/venv/bin
ExecStart=/opt/projekt2/venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8102 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Nach dem Anlegen:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now projekt2-api.service
sudo systemctl status projekt2-api.service --no-pager
```

`app:app` und der Port sind an das konkrete neue Projekt anzupassen.

## Webzugriff und Domain

Der Reverse Proxy auf dem App-Server erhaelt einen eigenen virtuellen Host fuer
die neue Subdomain und leitet nur an `127.0.0.1:8102` weiter. Bestehende
Proxy-Konfigurationen fuer `31-70-92-95.sslip.io` duerfen nicht ueberschrieben
werden.

Vor der Aenderung ist die vorhandene Nginx-Konfiguration zu lesen. Die neue
Konfigurationsdatei muss einen eigenen Namen haben, z. B.
`/etc/nginx/sites-available/projekt2.conf`, und wird erst nach
`nginx -t` aktiviert. TLS-Zertifikate werden pro Domain ausgestellt.

## Sicheres Deployment durch Codex

Ein Deployment darf nur diese Reihenfolge haben:

1. Lokale Tests und Syntaxpruefung ausfuehren.
2. Dateien nach `/opt/projekt2/app` kopieren.
3. Abhaengigkeiten im eigenen `/opt/projekt2/venv` installieren, falls sich
   die Lock- oder Requirements-Datei geaendert hat.
4. `sudo systemctl restart projekt2-api.service` ausfuehren.
5. `sudo systemctl is-active projekt2-api.service` und die letzten eigenen Logs
   pruefen.

Beispiel fuer einen minimalen Smoke-Test:

```powershell
ssh -i $env:PROJEKT2_SSH_KEY $env:PROJEKT2_SERVER `
  "sudo systemctl is-active projekt2-api.service; sudo journalctl -u projekt2-api.service -n 50 --no-pager"
```

Vor jedem Deployment gilt:

- Nie rekursiv nach `/opt` kopieren oder dort Dateien loeschen.
- Keine pauschalen `systemctl restart`-Aufrufe verwenden.
- Nie `nginx`, `mariadb`, `asterisk` oder einen `kosmos-*`-Dienst neu starten.
- Aenderungen an Firewall, Domains, Datenbanken oder Reverse Proxy nur nach
  vorheriger Pruefung und mit klar eingegrenzter Datei ausfuehren.

## Falls Projekt Zwei spaeter telefoniert

Dann ist ein separates Architekturgespraech erforderlich. Es braucht mindestens
einen eigenen Asterisk-Kontext, eigene SIP-Endpunkte, eigene Rufnummern- und
Caller-ID-Regeln, eigene Worker-Queues sowie eine getrennte Port- und
Sicherheitspruefung. Bis dahin bleibt `217.154.254.129` vollstaendig ausserhalb
des neuen Projekts.
