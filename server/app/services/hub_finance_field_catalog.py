"""Reviewed, intentionally compact Finance field catalogs."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HubFinanceField:
    key: str
    label: str
    display_type: str
    required: bool = False
    read_only: bool = False
    options: tuple[tuple[str, str], ...] = ()


ARTICLE_FIELDS = (
    HubFinanceField("name", "Name", "Text", required=True),
    HubFinanceField("sku", "Artikelnummer / SKU", "Text", required=True),
    HubFinanceField("status", "Status", "Auswahlliste", required=True, options=(("active", "Aktiv"), ("inactive", "Inaktiv"))),
    HubFinanceField("kind", "Art", "Auswahlliste", required=True, options=(("service", "Dienstleistung"), ("goods", "Ware"))),
    HubFinanceField("unit", "Einheit", "Text", required=True),
    HubFinanceField("net_price", "Verkaufspreis netto", "Waehrung", required=True),
    HubFinanceField("tax_rate", "MwSt.-Satz", "Auswahlliste", required=True, options=(("0", "0 %"), ("7", "7 %"), ("19", "19 %"))),
    HubFinanceField("revenue_account", "Erlöskonto", "Text", required=True),
    HubFinanceField("description", "Beschreibung", "Mehrzeilen"),
)


OFFER_FIELDS = (
    HubFinanceField("offer_number", "Angebotsnummer", "Autonummer", read_only=True),
    HubFinanceField("status", "Status", "Auswahlliste", required=True, options=(("draft", "Entwurf"), ("sent", "Versendet"), ("accepted", "Angenommen"), ("declined", "Abgelehnt"), ("expired", "Abgelaufen"))),
    HubFinanceField("offer_date", "Angebotsdatum", "Datum", required=True),
    HubFinanceField("valid_until", "Gültig bis", "Datum", required=True),
    HubFinanceField("customer", "Kunde", "Verknuepfung", required=True),
    HubFinanceField("contact", "Ansprechpartner", "Verknuepfung"),
    HubFinanceField("reference", "Referenz", "Text"),
    HubFinanceField("currency", "Währung", "Auswahlliste", required=True, options=(("EUR", "EUR"), ("CHF", "CHF"), ("USD", "USD"))),
    HubFinanceField("payment_terms", "Zahlungsbedingungen", "Auswahlliste", options=(("due_on_receipt", "Sofort fällig"), ("14_days", "14 Tage"), ("30_days", "30 Tage"))),
)
