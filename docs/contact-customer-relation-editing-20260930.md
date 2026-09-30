# Kontakt-Kundenverknüpfung in der Bearbeitungsmaske

## Verhalten

- Die rechte Karte `Hub-Daten > Übersicht` zeigt die aktuelle Kundenverknüpfung als Link und ist nicht mehr direkt bearbeitbar.
- Beim Bearbeiten eines Kontakts erscheint `Kunde` an der im Kontaktlayout festgelegten Position von `Kunde-Name`.
- Die Kundenauswahl verwendet dieselbe durchsuchbare Combobox wie die Finanzbelege.
- Eine Kundenverknüpfung ist in der Kontakt-Bearbeitungsmaske erforderlich.
- Kontaktfelder und eine geänderte Kundenverknüpfung werden gemeinsam über `contacts.update` gespeichert.
- Ein leerer oder nicht zugänglicher Zielkunde verwirft die gesamte Änderung.

## Kompatibilität

Interne Aufrufer, die bei `contacts.update` kein `new_customer_id` übergeben, ändern die bestehende Verknüpfung nicht. Die ältere Operation `contacts.link_customer` bleibt vorerst für bestehende Integrationen verfügbar, wird in der Kontaktansicht aber nicht mehr separat angeboten.
