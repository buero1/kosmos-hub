# Hub Release Playbook

## Ziel

Kleine sichtbare UI-Korrekturen sollen nach einem Neuladen der Hub-Seite schnell
verfuegbar sein. Aenderungen an Logik, Datenbank oder Konfiguration muessen
weiterhin ueber den vollstaendigen, pruefbaren Server-Release laufen.

## UI-Schnell-Release

Dieser Weg gilt ausschliesslich fuer Dateien unter `server/app/templates/` sowie
fuer CSS oder Browser-JavaScript, das in diesen Vorlagen enthalten ist.

1. Die betroffene Vorlage laden und die zugehoerigen Tests ausfuehren.
2. Die Aenderung lokal committen.
3. Nur die geaenderte Datei atomar auf den Server uebertragen: zuerst als
   temporaere Datei hochladen, dann auf dem Server auf den Zielnamen verschieben.
4. Den API-Dienst nicht neu starten. Die Jinja-Template-Aktualisierung ist im
   Hub aktiv; nach einem Neuladen rendert der Server die neue Vorlage.
5. Die betroffene Seite im Browser pruefen und den Git-Push direkt danach
   ausfuehren, damit Repository und Produktion denselben Stand dokumentieren.

Upload, atomarer Wechsel und Pruefung sollen moeglichst eine wiederverwendete
SSH-Verbindung nutzen. Das vermeidet die Wartezeit mehrerer neuer Verbindungen.

## Vollstaendiger Hub-Release

Dieser Weg ist zwingend fuer:

- Python-Dateien, API-Routen und serverseitige Logik
- Datenbankmigrationen oder Datenmodell-Aenderungen
- Abhaengigkeiten, Umgebungsvariablen und Dienst-Konfiguration
- Aenderungen, die gleichzeitig Vorlage und serverseitige Logik brauchen

Ablauf:

1. Aenderung lokal pruefen und committen.
2. Das vollstaendige App-Paket auf den Server uebertragen.
3. Dateirechte setzen, erforderliche Migrationen ausfuehren und den API-Dienst
   neu starten.
4. Dienststatus und `/healthz` pruefen.
5. Den Git-Push abschliessen, falls er nicht bereits erfolgt ist.

## Sicherheitsregeln

- Der Schnell-Release darf keine Python-, Datenbank- oder Konfigurationsdatei
  enthalten.
- Jede Serverdatei stammt aus einem lokalen Commit; direkte, nicht committedte
  Handarbeit auf dem Server ist nicht erlaubt.
- Ein fehlgeschlagener UI-Schnell-Release wird durch atomaren Rueckwechsel auf
  die vorherige Vorlage behoben.
- Laufende Wartungs- und Refresh-Jobs duerfen fuer reine UI-Aenderungen nicht
  durch einen API-Neustart unterbrochen werden.
