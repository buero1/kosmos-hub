# Auftrags-PDF: vollstaendiger Feldkatalog

Unter `Aktueller Auftrag` stehen alle Felder aus `ORDER_FIELDS` zur Verfuegung,
mit denselben Bezeichnungen wie in der Auftragsmaske. Der Katalog verwendet die
bestehende Token-Zuordnung der E-Mail-Vorlagen, nicht eine zweite manuelle Liste.
Die bisherigen Tokens und zusaetzlichen Summen-Platzhalter bleiben erhalten.

Die PDF-Projektion liest die formatierten Werte der Auftrags-Einzelansicht:
Datum/Uhrzeit, Auswahlbezeichnungen, Dezimalwerte sowie Namen und Belegnummern
statt interner IDs. Leere optionale Felder bleiben leer. Vorschauwerte sind
Beispiele und werden nicht in echte Belege uebernommen. Feldanordnung und
"Mehr anzeigen" schraenken die Platzhalterauswahl nicht ein.

Es werden nur katalogisierte Auftragsfelder freigegeben, keine beliebigen
verschluesselten Metadaten. Werte werden als Text escaped und genau einmal
ersetzt, auch in AGB. Andere PDF-Module, E-Mail-Kataloge und geschuetzte
Kundenfelder bleiben unveraendert. Bestehende Vorlagen/PDFs werden nicht
automatisch geaendert; ausgewaehlte Tokens gelten beim naechsten Erzeugen.

Tests: `tests/test_order_pdf_placeholders.py` prueft Katalogvollstaendigkeit,
Auswahl, Vorschau, echte Belegwerte, Layout-Unabhaengigkeit, leere Felder und
sichere Ersetzung. `tools/order-placeholder-live.cjs` prueft Suche/Einfuegen
auf Desktop und Mobil und bricht anschliessend ohne Speicherung ab.
