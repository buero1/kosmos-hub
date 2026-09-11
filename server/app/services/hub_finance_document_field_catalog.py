"""Reviewed Finance document fields used after articles and offers."""

from app.services.hub_finance_field_catalog import HubFinanceField


_CURRENCIES = (("EUR", "EUR"), ("CHF", "CHF"), ("USD", "USD"))
_PAYMENT_TERMS = (("due_on_receipt", "Sofort fällig"), ("7_days", "7 Tage"), ("14_days", "14 Tage"), ("30_days", "30 Tage"))


ORDER_FIELDS = (
    HubFinanceField("order_number", "Auftragsnummer", "Autonummer", read_only=True),
    HubFinanceField("status", "Status", "Auswahlliste", required=True, options=(("draft", "Entwurf"), ("confirmed", "Bestätigt"), ("completed", "Abgeschlossen"), ("cancelled", "Storniert"))),
    HubFinanceField("order_date", "Auftragsdatum", "Datum", required=True),
    HubFinanceField("customer", "Kunde", "Verknuepfung", required=True),
    HubFinanceField("contact", "Ansprechpartner", "Verknuepfung"),
    HubFinanceField("linked_offer", "Verknüpftes Angebot", "Verknuepfung"),
    HubFinanceField("reference", "Referenz", "Text"),
    HubFinanceField("currency", "Währung", "Auswahlliste", required=True, options=_CURRENCIES),
    HubFinanceField("payment_terms", "Zahlungsbedingungen", "Auswahlliste", options=_PAYMENT_TERMS),
)


INVOICE_FIELDS = (
    HubFinanceField("invoice_number", "Rechnungsnummer", "Autonummer", read_only=True),
    HubFinanceField("status", "Status", "Auswahlliste", required=True, options=(("draft", "Entwurf"), ("open", "Offen"), ("paid", "Bezahlt"), ("overdue", "Überfällig"), ("cancelled", "Storniert"))),
    HubFinanceField("invoice_date", "Rechnungsdatum", "Datum", required=True),
    HubFinanceField("due_date", "Fällig am", "Datum", required=True),
    HubFinanceField("customer", "Kunde", "Verknuepfung", required=True),
    HubFinanceField("contact", "Ansprechpartner", "Verknuepfung"),
    HubFinanceField("linked_order", "Verknüpfter Auftrag", "Verknuepfung"),
    HubFinanceField("currency", "Währung", "Auswahlliste", required=True, options=_CURRENCIES),
    HubFinanceField("payment_terms", "Zahlungsbedingungen", "Auswahlliste", options=_PAYMENT_TERMS),
    HubFinanceField("remaining_amount", "Restbetrag", "Waehrung", read_only=True),
)


RECURRING_INVOICE_FIELDS = (
    HubFinanceField("name", "Bezeichnung", "Text", required=True),
    HubFinanceField("status", "Status", "Auswahlliste", required=True, options=(("active", "Aktiv"), ("paused", "Pausiert"), ("ended", "Beendet"))),
    HubFinanceField("customer", "Kunde", "Verknuepfung", required=True),
    HubFinanceField("contact", "Ansprechpartner", "Verknuepfung"),
    HubFinanceField("start_date", "Startdatum", "Datum", required=True),
    HubFinanceField("end_date", "Enddatum", "Datum"),
    HubFinanceField("interval_unit", "Rhythmus", "Auswahlliste", required=True, options=(("month", "Monatlich"), ("quarter", "Vierteljährlich"), ("year", "Jährlich"))),
    HubFinanceField("interval_count", "Wiederholung alle", "Ganzzahl", required=True),
    HubFinanceField("next_invoice_date", "Nächstes Rechnungsdatum", "Datum", required=True),
    HubFinanceField("automatic_creation", "Automatisch erstellen", "Boolesch"),
    HubFinanceField("currency", "Währung", "Auswahlliste", required=True, options=_CURRENCIES),
    HubFinanceField("payment_terms", "Zahlungsbedingungen", "Auswahlliste", options=_PAYMENT_TERMS),
    HubFinanceField("late_fee", "Mahngebühr", "Waehrung"),
    HubFinanceField("system_payment_complete", "Systemzahlung erledigt", "Boolesch"),
)
