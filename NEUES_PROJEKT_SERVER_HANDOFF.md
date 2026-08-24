# Kosmos Hub: Server und Deployment

Dieses Dokument beschreibt die produktive, von der Telefonie getrennte
Kosmos-Hub-Installation auf dem gemeinsamen App-Server.

## Getrennte Systeme

| Bereich | Kosmos Hub | Geschuetzte Telefonie-App |
| --- | --- | --- |
| Server | `31.70.92.95` | `31.70.92.95` |
| Code | `/opt/kosmos-hub/app` | `/opt/kosmos-api` |
| Python-Umgebung | `/opt/kosmos-hub/venv` | eigene Umgebung unter `/opt/kosmos-api` |
| Dienst | `kosmos-hub-api.service` | `kosmos-api.service`, `kosmos-call-runner.service` |
| Oeffentliche URL | `https://kosmos-hub.31-70-92-95.sslip.io` | `https://31-70-92-95.sslip.io` |
| Interner Port | `127.0.0.1:8102` | eigener Port und eigene Nginx-Konfiguration |

Kosmos Hub laeuft als Linux-Benutzer `kosmos-hub`. Der Code ist fuer die
Gruppe `kosmos-hub-code` freigegeben. Die Telefonie-App, ihre Dienste und
`/etc/asterisk` gehoeren nicht zum Kosmos-Hub-Projekt und duerfen nie
geaendert oder neu gestartet werden.

## Deploy-Zugang

Der eingeschraenkte Benutzer `kosmoshubdeploy` ist der regulaere
Deployment-Zugang. Der private Schluessel liegt ausschliesslich lokal unter:

```text
%USERPROFILE%\.ssh\kosmos_hub_deploy_ed25519
```

Verbindungstest:

```powershell
$hubDeployKey = "$env:USERPROFILE\.ssh\kosmos_hub_deploy_ed25519"
ssh -i $hubDeployKey kosmoshubdeploy@31.70.92.95 "whoami; id; pwd"
```

Der Benutzer darf Code in `/opt/kosmos-hub/app` schreiben. Sein `sudo` ist
absichtlich auf den einen eigenen Dienst beschraenkt:

```powershell
sudo -n /usr/bin/systemctl restart kosmos-hub-api.service
sudo -n /usr/bin/systemctl is-active kosmos-hub-api.service
sudo -n /usr/bin/systemctl status kosmos-hub-api.service --no-pager
sudo -n /usr/bin/journalctl -u kosmos-hub-api.service -n 50 --no-pager
```

Keinen Root-Zugang fuer normale Hub-Deployments verwenden. Keine Dateien nach
`/opt/kosmos-api` kopieren und keine anderen Dienste als
`kosmos-hub-api.service` neu starten.

## Konfiguration

- Dienstdefinition: `/etc/systemd/system/kosmos-hub-api.service`
- Umgebungsdatei und Geheimnisse: `/etc/kosmos-hub/kosmos-hub.env`
- Reverse Proxy: `/etc/nginx/sites-enabled/kosmos-hub`
- Datenbankzugang: ausschliesslich ueber die Umgebungsdatei des Hubs

Die Umgebungsdatei und alle Zugangsdaten bleiben auf dem Server und werden
nicht in Git, Skripten oder Chat-Ausgaben gespeichert.

## Sicheres Deployment

1. Betroffene Dateien lokal testen.
2. Nur diese Dateien nach `/opt/kosmos-hub/app` hochladen.
3. Ausschliesslich `kosmos-hub-api.service` neu starten.
4. Dienststatus, eigene Logs und den betroffenen Hub-Endpunkt pruefen.

Beispiel fuer eine einzelne Python-Datei:

```powershell
$hubDeployKey = "$env:USERPROFILE\.ssh\kosmos_hub_deploy_ed25519"
scp -i $hubDeployKey .\server\app\services\site_registration.py `
  kosmoshubdeploy@31.70.92.95:/opt/kosmos-hub/app/server/app/services/site_registration.py
ssh -i $hubDeployKey kosmoshubdeploy@31.70.92.95 `
  "sudo -n /usr/bin/systemctl restart kosmos-hub-api.service; sudo -n /usr/bin/systemctl is-active kosmos-hub-api.service"
```

Vor einem groesseren Update den vorhandenen Code gezielt sichern. Nie
rekursiv ganze Ordner nach `/opt` kopieren oder dort Dateien loeschen.
