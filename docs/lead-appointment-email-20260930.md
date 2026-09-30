# Automatische Terminerinnerung für Leads

Der feste Workflow `lead-appointment-reminder` plant für jeden zukünftigen
Beratungstermin mit aktivierter Option `Termin-Erinnerung setzen?` genau eine
E-Mail am Vortag um 11:00 Uhr in der Zeitzone `Europe/Berlin`.

## Versandregeln

- Verwendet wird ausschließlich die aktive Vorlage `Terminerinnerung` im
  Ordner `Leads Hub` mit dem Kontext `Leads`.
- Absender ist das freigegebene Standardpostfach des aktiven Hub-Administrators.
- Empfänger ist die im Lead gespeicherte E-Mail-Adresse.
- Die gerenderte E-Mail wird verschlüsselt als dauerhafter Versandauftrag
  gespeichert und nach dem Versand mit dem Lead verknüpft.
- Eine Terminverschiebung storniert den alten Auftrag und plant einen neuen.
- `Termin-Erinnerung setzen? = Nein`, ein vergangener Termin oder ein
  deaktivierter Workflow storniert einen noch offenen Auftrag.
- Eine manuell stornierte Erinnerung wird für denselben Termin nicht erneut
  angelegt.
- Ist der geplante Zeitpunkt nach einem Ausfall bereits verstrichen, wird die
  E-Mail nur dann nachgeholt, wenn der Termin noch in der Zukunft liegt.
- Eine kurzfristig fehlende oder doppelte Vorlage löscht bereits korrekt
  geplante Erinnerungen nicht.

Die Automationskennung ist in `hub_scheduled_emails.automation_key` eindeutig.
Dadurch führen Neustarts oder wiederholte Prüfungen nicht zu Doppelversand.
