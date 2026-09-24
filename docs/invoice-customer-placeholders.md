# Rechnungs-PDF: Kundenbankdaten

Stand: 24.09.2026

Unter Quelle Kunde sind in Rechnungs-PDF-Vorlagen verfuegbar:

- IBAN: `${Customer.Iban}` aus dem Kundenfeld `iban`.
- BIC: `${Customer.Bic}` aus `bic`.
- Bankname: `${Customer.Bank}` aus `bank`.
- Kundennummer: bestehender Platzhalter `${Customer.CustomerNumber}`.

Keine automatische Aenderung vorhandener Vorlagen oder PDFs. Neue Platzhalter
werden beim erneuten Erzeugen einer Rechnung mit dem verknuepften Kunden
aufgeloest. Vorschauwerte sind fiktive Beispiele; fehlende Kundendaten bleiben
im Beleg leer. Eigene Firmenbankdaten (`Company.*`) bleiben getrennt.

Die IBAN wird ausschliesslich in der Rechnungsprojektion freigegeben. Der
allgemeine Kundenlesezugriff und der E-Mail-Platzhalterkatalog bleiben unveraendert.
Die bestehende Autorisierung fuer Rechnungserzeugung und PDF-Download sowie
die Kundensichtbarkeit gelten weiterhin. Wer eine solche Rechnung lesen darf,
kann die in der Vorlage ausgewaehlten Kundenbankdaten darin sehen.
Andere geschuetzte CRM-Felder werden nicht in die PDF-Projektion aufgenommen.

Pruefung: 131 gezielte Tests bestanden, einschliesslich Architekturkontrolle,
Finance-Operationen, Vorlagen, PDF-Erzeugung, Vorschau, eindeutiger Kundenquelle,
alter Feldbezeichnungen, Sonderzeichen und leerer/ungueltiger Bankdaten.
Kein neuer vollstaendiger Regressionstestlauf.

`tools/invoice-placeholder-live.cjs` prueft Suche und lokales Einfuegen aller
vier Platzhalter bei Desktop- und Mobilbreite. Schreibrequests werden blockiert;
die Vorschau wird abgebrochen, ohne bestehende Vorlagen zu speichern.

Release `68437a9` als vollstaendiges App-Archiv deployt: 391 Laufzeitdateien
gegen den vorherigen Commit geprueft, ausschliesslich die zwei beabsichtigten
Service-Dateien geaendert. Keine laufenden Wartungs-, Refresh-, WordPress- oder
PDF-Jobs vor dem Neustart; keine Migration. Dienst aktiv, internes und
oeffentliches healthz erfolgreich. Sicherung: `app-before-invoice-placeholders-20260924-152947`.

Live-Test bestanden bei 1263 und 390 Pixeln: alle vier Kundeneintraege gesucht,
lokal in den Editor eingefuegt und abgebrochen. Null Schreibrequests.
