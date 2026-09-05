"""Approved Zoho Account fields and the subforms that belong to them."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ZohoAccountField:
    key: str
    label: str
    api_name: str
    display_type: str
    required_for_identity: bool = False
    sensitive: bool = False
    subform_parent: bool = False


@dataclass(frozen=True)
class ZohoAccountSubform:
    key: str
    label: str
    parent_api_name: str
    fields: tuple[ZohoAccountField, ...]


# This catalog intentionally contains only the Account fields retained in the reviewed workbook.
ZOHO_ACCOUNT_FIELDS = (
    ZohoAccountField("record_id", "Eintrag-ID", "id", "ID", True),
    ZohoAccountField("work_domain", "Arbeitsdomain", "arbeitsdomain", "URL"),
    ZohoAccountField("work_domain_login", "Arbeitsdomain-Login", "arbeitsdomainLogin", "URL"),
    ZohoAccountField("sepa_grant_type", "Art SEPA-Erteilung", "Art_SEPA_Erteilung", "Auswahlliste"),
    ZohoAccountField("order_date", "Auftragsdatum", "Auftragsdatum", "Datum"),
    ZohoAccountField("bank", "Bank", "bank", "Einzelzeile"),
    ZohoAccountField("show_bank_details", "Bankverbindung zeigen", "Bankverbindung_zeigen", "Boolesch"),
    ZohoAccountField("rating_submitted", "Bewertung abgegeben", "Bewertung_abgegeben", "Boolesch"),
    ZohoAccountField("rating_email_received", "Bewertungs-E-Mail erhalten", "Bewertungs_Email_erhalten", "Boolesch"),
    ZohoAccountField("bic", "BIC", "bic", "Einzelzeile"),
    ZohoAccountField("previous_website", "Bisherige (alte) Website", "Bisherige_alte_Website", "URL"),
    ZohoAccountField("industry", "Branche", "Industry", "Auswahlliste"),
    ZohoAccountField("sepa_grant_date", "Datum SEPA-Erteilung", "Datum_SEPA_Erteilung", "Datum"),
    ZohoAccountField("duration_minutes", "Dauer in Minuten", "Dauer_in_Minuten", "Einzelzeile"),
    ZohoAccountField("dialfire_campaign_name", "Dialfire Kampagnename", "Dialfire_Kampagnename", "Einzelzeile"),
    ZohoAccountField("dialfire_campaign_stage", "Dialfire Kampagnenstufe", "Dialfire_Kampagnenstufe", "Auswahlliste"),
    ZohoAccountField("dialfire_follow_up_at", "Dialfire WV-Datum", "Dialfire_WV_Datum", "DatumZeit"),
    ZohoAccountField("dialfire_api_token", "Dialfire-API-Token", "Dialfire_API_Token", "Einzelzeile", sensitive=True),
    ZohoAccountField("dialfire_comment", "Dialfire-Comment", "Dialfire_Comment", "Multizeilen (klein)"),
    ZohoAccountField("dialfire_result", "Dialfire-Ergebnis", "Dialfire_Ergebnis", "Auswahlliste"),
    ZohoAccountField("dialfire_id", "Dialfire-ID", "Dialfire_ID", "Einzelzeile"),
    ZohoAccountField("dialfire_campaign_id", "Dialfire-Kampagne-ID", "Dialfire_Kampagne_ID", "Einzelzeile"),
    ZohoAccountField("dialfire_season_time", "Dialfire-Saisonzeit", "Dialfire_Saisonzeit", "Auswahlliste"),
    ZohoAccountField("dialfire_status", "Dialfire-Status", "Dialfire_Status", "Auswahlliste"),
    ZohoAccountField("dialfire_follow_up_owner", "Dialfire-WV-Besitzer", "Dialfire_WV_Besitzer", "Auswahlliste"),
    ZohoAccountField("dialfire_follow_up_formula", "Dialfire-WV-Formel", "Dialfire_WV_Formel", "Einzelzeile"),
    ZohoAccountField("dialfire_follow_up_note", "Dialfire-WV-Notiz", "Dialfire_WV_Notiz", "Multizeilen (klein)"),
    ZohoAccountField("facebook", "Facebook", "Facebook", "URL"),
    ZohoAccountField("cancelled_at", "Gekündigt am", "Gek_ndigt_am", "Datum"),
    ZohoAccountField("google_calendar", "Google Kalender", "Google_Kalender", "Einzelzeile"),
    ZohoAccountField("iban", "IBAN", "iban", "Einzelzeile (Verschlüsselt)", sensitive=True),
    ZohoAccountField("instagram", "Instagram", "Instagram", "URL"),
    ZohoAccountField("annual_cycle", "Jahresturnus", "Jahresturnus", "Formel"),
    ZohoAccountField("account_holder", "Kontoinhaber", "kontoinhaber", "Einzelzeile"),
    ZohoAccountField("customer_type", "Kunde Typ", "Account_Type", "Auswahlliste"),
    ZohoAccountField("customer_address", "Kunde-Anschrift", "Kunde_Anschrift", "Einzelzeile"),
    ZohoAccountField("customer_name", "Kunde-Name", "Account_Name", "Einzelzeile", True),
    ZohoAccountField("customer_name_address", "Kunde-Name-Anschrift", "Kunde_Name_Anschrift", "Einzelzeile"),
    ZohoAccountField("customer_number", "Kunde-Nummer", "Account_Number", "Long-Ganzzahl (Eindeutig)"),
    ZohoAccountField("cancellation_date", "Kündigungsdatum", "kuendigungsdatum", "Datum"),
    ZohoAccountField("lead_creator", "Lead-Ersteller", "Lead_Ersteller", "Einzelzeile"),
    ZohoAccountField("last_update_date", "Letztes Update-Datum", "Letztes_Update_Datum", "Datum"),
    ZohoAccountField("send_options_to_wordpress", "Options an WP senden", "Options_an_WP_senden", "Boolesch"),
    ZohoAccountField("source", "Quelle", "Quelle", "Einzelzeile"),
    ZohoAccountField("real_cookie_done", "Real Cookie erledigt", "Real_Cookie_erledigt", "Boolesch"),
    ZohoAccountField("billing_state", "Rechnungsadresse - Bundesland", "Billing_State", "Einzelzeile"),
    ZohoAccountField("billing_country", "Rechnungsadresse - Land", "Billing_Country", "Einzelzeile"),
    ZohoAccountField("billing_postal_code", "Rechnungsadresse - PLZ", "Billing_Code", "Einzelzeile"),
    ZohoAccountField("billing_city", "Rechnungsadresse - Stadt", "Billing_City", "Einzelzeile"),
    ZohoAccountField("billing_street", "Rechnungsadresse - Straße", "Billing_Street", "Einzelzeile"),
    ZohoAccountField("account_status", "Status", "status", "Auswahlliste", True),
    ZohoAccountField("tag", "Tag", "Tag", "Einzelzeile"),
    ZohoAccountField("phone", "Tel.", "Phone", "Telefon"),
    ZohoAccountField("appointment_reminder", "Termin-Erinnerung setzen?", "Termin_Erinnerung_setzen", "Boolesch"),
    ZohoAccountField("appointment_at", "Terminzeit", "Terminzeit", "DatumZeit"),
    ZohoAccountField("update_date", "Update-Datum", "Update_Datum", "Datum"),
    ZohoAccountField("contract_start", "Vertragsbeginn", "Vertragsbeginn", "Datum"),
    ZohoAccountField("contract_duration", "Vertragsdauer (in Jahren)", "Vertragsdauer", "Einzelzeile"),
    ZohoAccountField("website", "Webseite", "Website", "URL"),
    ZohoAccountField("website_package", "Website-Paket", "Website_Paket", "Auswahlliste"),
    ZohoAccountField("important_info", "Wichtige Infos", "Wichtige_Infos", "Multizeilen (klein)"),
    ZohoAccountField("ws_updates", "WS-Updates", "WS_Updates", "Unterformular", subform_parent=True),
)


ZOHO_ACCOUNT_SUBFORMS = (
    ZohoAccountSubform(
        "email_setup",
        "Email-Einricht./ Account",
        "Email_Einricht_Account",
        (
            ZohoAccountField("choice", "Auswahl", "Auswahlliste_1", "Auswahlliste"),
            ZohoAccountField("username", "Benutzername", "Benutzername", "Einzelzeile"),
            ZohoAccountField("email_address", "Email-Adresse", "Email_Adresse", "Einzelzeile"),
            ZohoAccountField("note", "Notiz", "Notiz", "Einzelzeile"),
            ZohoAccountField("password", "Passwort", "Passwort", "Einzelzeile", sensitive=True),
            ZohoAccountField("url", "URL", "URL", "URL"),
        ),
    ),
    ZohoAccountSubform(
        "ws_updates",
        "WS-Updates",
        "WS_Updates",
        (ZohoAccountField("update_date", "Update-Datum", "Update_Datum", "Datum"),),
    ),
)
