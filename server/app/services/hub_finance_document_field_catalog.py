"""Reviewed Finance document fields used after articles and offers."""

from app.services.hub_finance_field_catalog import HubFinanceField


_CURRENCIES = (("EUR", "EUR"), ("CHF", "CHF"), ("USD", "USD"))
_PAYMENT_TERMS = (("due_on_receipt", "Sofort fällig"), ("7_days", "7 Tage"), ("14_days", "14 Tage"), ("30_days", "30 Tage"))

_ORDER_CANCELLATION_PERIODS = (
    ("1 Monat vor Ablauf, Verlängerung um 1 Jahr", "1 Monat vor Ablauf, Verlängerung um 1 Jahr"),
    ("1 Woche vor Ablauf, Verlängerung um 1 Jahr", "1 Woche vor Ablauf, Verlängerung um 1 Jahr"),
    ("1 Monat zum Monatsende", "1 Monat zum Monatsende"),
    ("Noch nicht eingetragen", "Noch nicht eingetragen"),
)

_ORDER_INTAKE_TYPES = (
    ("Per Tel. ohne Mitschnitt", "Per Tel. ohne Mitschnitt"),
    ("Schriftlich", "Schriftlich"),
    ("Per Tel. mit Mitschnitt", "Per Tel. mit Mitschnitt"),
    ("Per Onlineformular", "Per Onlineformular"),
    ("Angebot angenommen", "Angebot angenommen"),
    ("Per Email angenommen", "Per Email angenommen"),
)

_ORDER_WON_BY = tuple(
    (name, name)
    for name in (
        "N. Baydar",
        "Stefanie Lier",
        "Birgit Beseler",
        "Volker Mohrau",
        "Faber",
        "Schimazek",
        "Michael Langhammer",
        "David Faber",
        "J.M. Salah",
        "Rudi Schmidt",
        "Ulrike Baier",
    )
)


ORDER_FIELDS = (
    HubFinanceField("order_number", "Auftragsnummer", "Autonummer", read_only=True),
    HubFinanceField("order_name", "Auftrag-Name", "Text", required=True),
    HubFinanceField("status", "Status", "Auswahlliste", required=True, options=(("draft", "Entwurf"), ("confirmed", "Bestätigt"), ("completed", "Abgeschlossen"), ("cancelled", "Storniert"))),
    HubFinanceField("order_date", "Auftragsdatum", "Datum", required=True),
    HubFinanceField("customer", "Kunde", "Verknuepfung", required=True),
    HubFinanceField("contact", "Ansprechpartner", "Verknuepfung", required=True),
    HubFinanceField("linked_offer", "Verknüpftes Angebot", "Verknuepfung"),
    HubFinanceField("outdoor_sales", "Aussendienst", "Auswahlliste", options=(("N.Baydar", "N.Baydar"), ("Lukas Reich", "Lukas Reich"))),
    HubFinanceField("contract_start", "Vertragsbeginn", "Datum"),
    HubFinanceField("contract_note", "Vertragsbemerkung", "Text"),
    HubFinanceField("contract_term", "Auftragslaufzeit", "Auswahlliste", required=True, options=(("12", "12"), ("24", "24"), ("1", "1"))),
    HubFinanceField("payment_method", "Zahlungsart", "Auswahlliste", required=True, options=(("Lastschrift", "Lastschrift"), ("Rechnung", "Rechnung"))),
    HubFinanceField("payment_frequency", "Zahlweise", "Auswahlliste", required=True, options=(("Monatlich", "Monatlich"), ("Jährlich", "Jährlich"))),
    HubFinanceField("cancellation_period", "Kündigungsfrist", "Auswahlliste", required=True, options=_ORDER_CANCELLATION_PERIODS),
    HubFinanceField("domain_request", "Domainwunsch", "Text"),
    HubFinanceField("order_intake_type", "Auftragsaufnahme Art", "Auswahlliste", required=True, options=_ORDER_INTAKE_TYPES),
    HubFinanceField("contract_duration_years", "Vertragsdauer (in Jahren)", "Dezimalzahl"),
    HubFinanceField("cancellation_date", "Kündigungsdatum", "Datum"),
    HubFinanceField("order_won_by", "Auftrag erzielt von", "Auswahlliste", options=_ORDER_WON_BY),
    HubFinanceField("agent_order_rating", "Auftragswertung Agent", "Auswahlliste", options=(("100%", "100%"), ("50%", "50%"), ("Andere", "Andere"))),
    HubFinanceField("created_time", "Zeitpunkt der Erstellung", "DatumZeit", read_only=True),
    HubFinanceField("modified_time", "Zeitpunkt der Änderung", "DatumZeit", read_only=True),
)


INVOICE_FIELDS = (
    HubFinanceField("invoice_number", "Rechnungsnummer", "Autonummer", read_only=True),
    HubFinanceField("status", "Status", "Auswahlliste", required=True, options=(("draft", "Entwurf"), ("open", "Offen"), ("paid", "Bezahlt"), ("overdue", "Überfällig"), ("cancelled", "Storniert"))),
    HubFinanceField("invoice_date", "Rechnungsdatum", "Datum", required=True),
    HubFinanceField("due_date", "Fällig am", "Datum", required=True),
    HubFinanceField("customer", "Kunde", "Verknuepfung", required=True),
    HubFinanceField("contact", "Ansprechpartner", "Verknuepfung", required=True),
    HubFinanceField("linked_order", "Verknüpfter Auftrag", "Verknuepfung"),
    HubFinanceField("currency", "Währung", "Auswahlliste", required=True, options=_CURRENCIES),
    HubFinanceField("payment_terms", "Zahlungsziel", "Auswahlliste", options=_PAYMENT_TERMS),
    HubFinanceField("remaining_amount", "Restbetrag", "Waehrung", read_only=True),
)


DUNNING_FIELDS = (
    HubFinanceField("dunning_number", "Mahnungsnummer", "Autonummer", read_only=True),
    HubFinanceField(
        "status",
        "Status",
        "Auswahlliste",
        required=True,
        options=(
            ("debit_retry", "Erneuter Abbuchung nach Lastschrift-Retour"),
            ("second_debit_return", "Zahlungsaufforderung nach zweiter Lastschrift-Retour"),
            ("payment_reminder", "Zahlungserinnerung"),
            ("dunning", "Mahnung"),
            ("final_dunning", "Letzte Mahnung"),
        ),
    ),
    HubFinanceField("dunning_date", "Mahnungsdatum", "Datum", required=True),
    HubFinanceField("due_date", "Zahlungsfrist bis", "Datum", required=True),
    HubFinanceField("customer", "Kunde", "Verknuepfung", required=True),
    HubFinanceField("contact", "Ansprechpartner", "Verknuepfung", required=True),
    HubFinanceField("linked_invoice", "Verknüpfte Rechnung", "Verknuepfung", required=True),
    HubFinanceField("currency", "Währung", "Auswahlliste", required=True, options=_CURRENCIES),
)


RECURRING_INVOICE_FIELDS = (
    HubFinanceField("name", "Bezeichnung", "Text", required=True),
    HubFinanceField("status", "Status", "Auswahlliste", required=True, options=(("active", "Aktiv"), ("paused", "Pausiert"), ("ended", "Beendet"))),
    HubFinanceField("customer", "Kunde", "Verknuepfung", required=True),
    HubFinanceField("contact", "Ansprechpartner", "Verknuepfung", required=True),
    HubFinanceField("start_date", "Startdatum", "Datum", required=True),
    HubFinanceField("end_date", "Enddatum", "Datum"),
    HubFinanceField("interval_unit", "Rhythmus", "Auswahlliste", required=True, options=(("month", "Monatlich"), ("quarter", "Vierteljährlich"), ("year", "Jährlich"), ("month_end", "Monatsende"), ("month_start", "Monatsanfang"), ("custom", "Benutzerdefiniert"))),
    HubFinanceField("custom_interval", "Rhythmus Benutzerdefiniert", "Benutzerdefiniertes Intervall"),
    HubFinanceField("next_invoice_date", "Nächstes Rechnungsdatum", "Datum", required=True),
    HubFinanceField("currency", "Währung", "Auswahlliste", required=True, options=_CURRENCIES),
    HubFinanceField("payment_due", "Zahlungsziel", "Zahlungsziel"),
)
