# Kundenwerte ans Firmenprofil

Stand 24.09.2026. Drei-Punkte-Menue der Kunden-Einzelansicht:
Daten ans Firmenprofil senden. Das alte Bool-Feld Options an WP senden ist aus
Anzeige, Bearbeitung und Layoutkatalog entfernt; historische Rohdaten bleiben
im verschluesselten Profil erhalten.

## Ziel und Rechte

Standard: Website, nur bei leerem Wert Arbeitsdomain, danach Arbeitsdomain-Login.
Loginpfade dienen nur der Domainermittlung, keine beliebigen URL-Aufrufe.
Nicht leere ungueltige/nicht verbundene Ziele fuehren zu einem Fehler, nicht zu
stillem Fallback. Nur eindeutig verifizierte, diesem Kunden zugeordnete Sites
mit aktiver eigener HTTPS-Bridge und Kunden-/Website-Bearbeitungsrecht.
Explizite alternative Ziele sind auf diese drei Kundenfelder beschraenkt.

Content Kit ab0.3.3, aktive aktuelle Bridge und WP Abilities API (6.9+) erforderlich.
Kein pauschaler Plugin-Rollout und keine automatische Synchronisation.

## Ablauf und Architektur

Rechte Sidebar zeigt Feld, bestehenden Website-Wert und geplanten Kundenwert.
Veraenderte zuordenbare Felder vorgewaehlt; Auswahl/Abwahl, Abbruch ohne Versand.
Unbekannte/entfernte Felder, leere Werte, Bilder und Struktur bleiben unveraendert.
Zuordnung ueber stabile IDs, einschliesslich der drei Referenz-Textfelder, nicht
ueber veraenderbare Beschriftungen. Kundenname, Rechnungsadresse, Telefon,
Website und daraus ableitbare Adress-/Telefonlink-Felder werden zugeordnet.
E-Mail nur bei eindeutigen eigenen Kundenfeldern email/secondary_email, nicht
aus Zugangsdaten oder willkuerlich aus mehreren Kontakten geraten.

UI und Agent benutzen customers.website_profile.preview und
wordpress.company_profile.send. Bestaetigungstoken verschluesselt, 30 Minuten,
an Benutzer, Kunde, Quelldaten und Zielidentitaet gebunden. Durable Job prueft
Rechte/Quelle/Identitaet erneut. WP-Revision verhindert veraltetes Ueberschreiben.
Store prueft atomar unter DB-Sperre. Keine globalen WP-Administratorrechte.
TLS, feste gespeicherte Bridge, keine Redirects, Antworten auf4MiB begrenzt.
Remote-Fehler im neuen Transportweg redigiert; Job/Audit enthaelt IDs statt Werte.
Unklare Ergebnisse werden nicht automatisch erneut gesendet.

## Nachweise

- Gezielt112 Python-Tests bestanden, inklusive Rechte, Prioritaet, verschluesselte
  Queue, Widerruf, Quellen-/UUID-Aenderung, Konflikt, CSRF und Architektur.
- Architekturkontrolle lehnte beide neuen Routen sowie geaenderte Detailroute
  vor dem ausdruecklichen Vertragsreview ab; danach bestanden.
- Desktop1300px / Mobil390px: gemeinsame Drawer-CSS, Feldwahl, Zielwechsel,
  escaped Remote-Text, Erfolg, Fehler, erneutes Oeffnen und Abbruch getestet.
- Content Kit:388 PHP-Vertragschecks, reale Testsite2 auf0.3.3 aktualisiert.
  Native signierte Lese-/Schreibprobe erfolgreich, stale revision HTTP409.
  Urspruengliche Testwerte und Definitionen danach vollstaendig wiederhergestellt.
- Release0.3.3 CI7.4/8.2/8.4 erfolgreich, oeffentliches ZIP/Metadaten SHA-identisch
  zum getesteten Paket. Keine Kundenwebsite aktualisiert, keine Kundendaten gesendet.

Grenzen: keine automatische Zuordnung frei erfundener Website-Felder, keine
Medien-/Gruppen-/Elementor-Strukturaenderung. Multisite/DB-Cluster nicht freigegeben.
Volltest und Live-Hub-Abnahme werden im Abschlussvermerk dokumentiert.

## Abschlussvermerk

- Hub-Release-Snapshot3aa0a7a auf release/customer-website-profile-20260924,
  privates Remote gepusht. Isolierter Git-Index erhielt den vorhandenen
  main-Arbeitsstand; keine fremden Aenderungen verworfen oder mitgestagt.
- Vor Release normalisierte Laufzeitdateien mit Produktion verglichen:
  nur16 eigene neue/geaenderte Dateien. Vollstaendiges App-Archiv deployt,
  keine Migration. Vor Neustart keine laufenden Wartungs-/WordPress-Jobs.
- Erster Gesundheitscheck benutzte irrtuemlich Port8000 und loeste den
  vorgesehenen Rueckfall aus. Neuer Start mit tatsaechlichem Dienstport8102
  erfolgreich; /healthz meldet ok, Dienst aktiv. Vorherige App gesichert.
- Live-Kunde8: Menue vorhanden, altes Feld unsichtbar, Sidebar auf Desktop/Mobil
  sichtbar, Abbruch erzeugt null Sendeanfragen. Fehlende neue Content-Kit-Ability
  ergibt sicheren Versionshinweis/409 und deaktivierte Uebertragung.
- Gesamtlauf:2032 bestanden,6 uebersprungen,2 veraltete Prueferwartungen.
  Feldzaehlung60->59 korrigiert; Mail-Send-Verbot auf emails.* beschraenkt, statt
  die neue Firmenprofil-Sendeaktion mitzusperren. Beide betroffenen Testdateien
  danach45 bestanden. Kein zweiter vollstaendiger Lauf behauptet.
- Zusaetzliche Transport-Negativtests: keine Redirects, begrenzte Antworten,
  keine vom Remote zurueckgespiegelten Privatwerte in Fehler/Audit. Zusammen
  mit Architekturtests14 bestanden; bisherige gezielte112 sowie Browserchecks
  ebenfalls bestanden.
- Content Kit0.3.3 oeffentlich veroeffentlicht, aber auf Kundenwebsites nicht
  automatisch installiert. Auf Testsite2 getestet, Originalwerte wiederhergestellt.
