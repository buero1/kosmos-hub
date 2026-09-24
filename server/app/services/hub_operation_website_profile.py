"""Shared, read-only projection used by the customer drawer and the agent."""
from app.services.hub_operations import HubQuery, HubOperationInputField as Field, register_query
from app.services.hub_record_access import identifier
from app.services.customer_website_profile import preview


register_query(HubQuery("customers.website_profile.preview",
    "Firmenprofil-Vorschau lesen, nichts senden. Standardziel: Website, sonst Arbeitsdomain, sonst Arbeitsdomain-Login. "
    "Vorschautoken und ausgewaehlte Feld-IDs fuer wordpress.company_profile.send verwenden. "
    "Optional edited_values_json (JSON-Objekt Feld-ID zu Text) fuer bestaetigte Anpassungen der editierbaren Felder. "
    "contacts liefert nur erlaubte verknuepfte Kontakte; contact_id waehlt bei der Sendeaktion den Ansprechpartner "
    "und dessen E-Mail-/Email-Link-Vorgaben. Ohne contact_id wird der erste Kontakt verwendet. "
    "Nur das Firmenprofil wird geaendert, nicht der Kunde. Leere Werte loeschen nichts.",
    (Field("customer_id", "Kunden-ID", required=True, max_length=18), Field("site_id", "Optionales alternatives Ziel aus der Vorschau", max_length=18)),
    lambda service, values: preview(service, identifier(values.get("customer_id", "")), identifier(values["site_id"]) if values.get("site_id") else None)))
