# Google Suche in Detailansichten

- Kunden und Leads: Drei-Punkte-Menue > Google Suche, auch mit reinem Lesezugriff.
- Kunde: Kundenname + Rechnungsstrasse/Hausnummer + Rechnungs-PLZ + Rechnungsort.
- Lead: Firma + Strasse/Hausnummer + PLZ + Stadt; ohne Firma wird der Leadname genutzt.
- Fehlende Angaben werden ausgelassen. Sonderzeichen werden als ein Suchparameter codiert.
- Die Suche nutzt gespeicherte Werte, unabhaengig von Reihenfolge und Aufteilung des Feldlayouts.
- Ein normaler Link oeffnet Google in einem neuen Tab mit `noopener noreferrer`.
- Der Hub sendet beim Laden der Seite keine Suchanfrage; nur ein ausdruecklicher Klick oeffnet Google.
- Es werden keine E-Mails, Notizen, Bankdaten oder internen Kennungen in den Suchtext aufgenommen.
- Bearbeitungs-/Loeschrechte und bestehende Menueaktionen bleiben unveraendert.

## Pruefung

`tests/test_google_search.py` prueft URL-Codierung, fehlende Werte, importierte und lokale
Kundenprofile, Lead-Fallback sowie beide Menues mit und ohne Verwaltungsrechte.
`tools/google-search-live.cjs` prueft Desktop und Mobilansicht sowie das Oeffnen neuer Tabs.
Dabei werden saemtliche Google-Anfragen abgefangen und Schreibzugriffe blockiert.
