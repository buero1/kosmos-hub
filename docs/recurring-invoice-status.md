# Periodische Rechnungen: Status und naechster Termin

- Nur `active` wird automatisch verarbeitet. `paused` und `ended` bleiben gesperrt.
- Beim Speichern von Pausiert/Beendet werden sowohl `next_invoice_date` im
  verschluesselten Datensatz als auch `hub_next_run_on` geleert. Alte oder manuell
  mitgeschickte Termine werden bei diesen Statuswerten ignoriert.
- Die Bearbeitungsmaske leert das Datum sofort und macht es schreibgeschuetzt.
  Bei Aktivierung ist ein neuer Termin erforderlich; es wird kein alter Termin
  stillschweigend wiederhergestellt. Abbrechen stellt den gespeicherten Zustand her.
- Dies gilt auch bei automatisch erreichtem Enddatum und beim Import gestoppter
  Serien. Vorhandene pausierte/beendete Serien werden beim Cursor-Abgleich
  bereinigt. Dieser Abgleich erzeugt selbst keine Rechnungen und aendert keine
  aktiven Termine, Positionen, Zuordnungen oder bereits erstellten Rechnungen.
- Schreiben, Abgleich und Erstellung pruefen die Serie unter einer Zeilensperre.
  Eine bereits vor dem Pausieren erstellte Rechnung wird dadurch nicht geloescht.

## Pruefung

`tests/test_recurring_invoice_status.py` deckt CRUD/Teilupdates, Lesewerte,
Reaktivierung, Altbestand, automatische Beendigung und Import ab.
`HUB_RECURRING_STATUS_ARTIFACT_DIR=tmp/recurring-status-browser` exportiert lokale
HTML-Fixtures; `node tests/js/recurring_invoice_status.cjs` prueft die echte
Bearbeitungsmaske auf Desktop und Mobil, ohne Datensaetze zu speichern.
