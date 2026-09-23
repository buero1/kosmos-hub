"""Reviewed Zoho Contact fields used for creation and synchronization."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ZohoContactField:
    key: str
    label: str
    api_name: str
    display_type: str
    required: bool = False
    required_on_create: bool = False
    options: tuple[tuple[str, str], ...] = ()


# Account_Name is deliberately not part of this catalog. The Hub always derives
# that link from the customer selected in the form, never from submitted text.
ZOHO_CONTACT_FIELDS = (
    ZohoContactField("salutation", "Anrede", "Salutation", "Auswahlliste", required_on_create=True,
                     options=(("Frau", "Frau"), ("Herr", "Herr"), ("Divers", "Divers"))),
    ZohoContactField("letter_salutation", "Briefanrede", "Briefanrede", "Einzelzeile"),
    ZohoContactField("first_name", "Vorname", "First_Name", "Einzelzeile"),
    ZohoContactField("last_name", "Nachname", "Last_Name", "Einzelzeile", required=True),
    ZohoContactField("title", "Titel", "Title", "Einzelzeile"),
    ZohoContactField("function", "Funktion", "Funktion", "Auswahlliste"),
    ZohoContactField("email", "E-Mail", "Email", "E-Mail"),
    ZohoContactField("secondary_email", "Zweite E-Mail-Adresse", "Secondary_Email", "E-Mail"),
    ZohoContactField("third_email", "Dritte E-Mail-Adresse", "Dritte_E_Mail_Adresse", "E-Mail"),
    ZohoContactField("phone", "Tel.", "Phone", "Telefon"),
    ZohoContactField("mobile", "Mobil", "Mobile", "Telefon"),
    ZohoContactField("alternate_phone", "Telefon alternativ", "Other_Phone", "Telefon"),
    ZohoContactField("private_phone", "Telefon privat", "Home_Phone", "Telefon"),
    ZohoContactField("assistant_phone", "Telefon Sekr.", "Asst_Phone", "Telefon"),
    ZohoContactField("mailing_street", "Postadresse Straße", "Mailing_Street", "Einzelzeile"),
    ZohoContactField("mailing_postal_code", "Postadresse PLZ", "Mailing_Zip", "Einzelzeile"),
    ZohoContactField("mailing_city", "Postadresse Stadt", "Mailing_City", "Einzelzeile"),
    ZohoContactField("mailing_state", "Postadresse Bundesland", "Mailing_State", "Einzelzeile"),
    ZohoContactField("mailing_country", "Postadresse Land", "Mailing_Country", "Einzelzeile"),
    ZohoContactField("customer_status", "Status Kunde", "Status_Kunde", "Einzelzeile"),
    ZohoContactField("tag", "Tag", "Tag", "Einzelzeile"),
)


def contact_fields(*, creating: bool = False) -> tuple[ZohoContactField, ...]:
    return tuple(replace(field, required=field.required or (creating and field.required_on_create)) for field in ZOHO_CONTACT_FIELDS)
