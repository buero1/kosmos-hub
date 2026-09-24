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
Neue Werte sind bearbeitbar, auch ohne automatisch passende Kundenangabe.
Bestehende unbekannte Textfelder duerfen manuell befuellt werden; nicht im
authentifizierten Schema vorhandene Felder, leere Werte, Bilder und Struktur
bleiben unveraendert. Nur ausgewaehlte Werte werden uebertragen.
Zuordnung ueber stabile IDs, einschliesslich der drei Referenz-Textfelder, nicht
ueber veraenderbare Beschriftungen. Kundenname, Rechnungsadresse, Telefon,
Website und daraus ableitbare Adress-/Telefonlink-Felder werden zugeordnet.
E-Mail nur bei eindeutigen eigenen Kundenfeldern email/secondary_email, nicht
aus Zugangsdaten oder willkuerlich aus mehreren Kontakten geraten.
Vollstaendige Firmierung verwendet standardmaessig den Firmennamen.
Firmenname-Anschrift wird frisch aus Firmenname und Anschrift zusammengesetzt,
nicht aus einem importierten Formelwert. Ansprechpartner ist der erste fuer
den Benutzer sichtbare verknuepfte Kontakt, alphabetisch wie in der Kontaktliste
(bei gleichen Namen nach ID). Ohne lesbaren Kontakt bleibt das Feld leer.
Manuelle Aenderungen im Dialog gelten nur fuer die Website, nicht fuer das CRM.

UI und Agent benutzen customers.website_profile.preview und
wordpress.company_profile.send. Bestaetigungstoken verschluesselt, 30 Minuten,
an Benutzer, Kunde, Quelldaten und Zielidentitaet gebunden. Durable Job prueft
Rechte/Quelle/Identitaet erneut. WP-Revision verhindert veraltetes Ueberschreiben.
Store prueft atomar unter DB-Sperre. Keine globalen WP-Administratorrechte.
TLS, feste gespeicherte Bridge, keine Redirects, Antworten auf4MiB begrenzt.
Remote-Fehler im neuen Transportweg redigiert; Job/Audit enthaelt IDs statt Werte.
Unklare Ergebnisse werden nicht automatisch erneut gesendet.

Erweiterung24.09.2026: Vorschautokenv2 bindet auch erlaubte Feldtypen/Laengen
und die Kontaktquelle. Optionale edited_values_json enthalten ausschliesslich
ausgewaehlte Feld-IDs und Texte. Gemeinsame Validierung vor Queue und Versand,
inklusive Email/URL/Telefon/Datum/Datum-Zeit und Payload-Limit. Eingabefehler422
lassen den Dialog samt Entwurf korrigierbar offen; bei unklarem Transportergebnis
bleiben die Eingaben sichtbar, aber Wiederholung ist gesperrt. Content Kit0.3.3
unterstuetzt diese Werte bereits; keine neue Plugin-Version erforderlich.

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

## Nachtrag: Editierbare Vorschau

- Release8be8573 vom24.09.2026: Werte im Dialog editierbar, Firmierung aus
  Firmenname, Firmenname-Anschrift ohne alten Formelwert, Ansprechpartner aus
  erstem erlaubten Kontakt. Nur Website-Werte werden angepasst, keine CRM-Daten.
- 84 gezielte Python-Tests und11 Architekturtests bestanden. Die Architekturkontrolle
  verlangte zunaechst explizit den Review der geaenderten Send-Route; gemeinsamer
  Gateway, CSRF, Schema-Grenzen, Validierung vor Queue und Datenschutz geprueft.
- Gesamtlauf nach der Aenderung:2070 bestanden,6 uebersprungen (535,06 Sekunden).
- Desktop1300/Mobil390: editierbare Texte, Mehrzeiler, Email/Datum, Auswahl,
  selektive Sendedaten, Entwurfserhalt bei422, kein Wiederholen bei unklarem
  Transportergebnis, Abbruch und Zielwechsel bestanden.
- Normalisierter Produktionsvergleich: nur7 beabsichtigte Laufzeitdateien
  unterschieden sich. Vollstaendiges App-Archiv mit gepruefter SHA deployt,
  vorher keine laufenden Wartungs-/WordPress-Jobs, keine Migration.
  Vorheriger App-Stand als app-before-profile-editable-8be8573 gesichert.
- Privater Release-Branch gepusht; vorhandener main-Index unveraendert.
  Lokales und oeffentliches /healthz ok. Live-Dialog Kunde1: Vorschau200,
  Firmen-/Kontaktvorgaben geprueft, Felder editierbar, Desktop/Mobil erreichbar.
  Alle Sendeanfragen im Browsertest gesperrt; null Sendeanfragen ausgeloest.
  Kein Kunden-Firmenprofil geaendert. Kein neues Plugin-Release notwendig.
