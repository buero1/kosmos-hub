"""The reviewed field schema for Hub cases (Fälle)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HubCaseField:
    key: str
    label: str
    api_name: str
    display_type: str
    required: bool = False
    read_only: bool = False
    options: tuple[str, ...] = ()


# This catalog intentionally mirrors the reviewed spreadsheet instead of the
# broader Zoho Cases schema. It is the single source for forms and detail views.
HUB_CASE_FIELDS = (
    HubCaseField("case_number", "Fall-Nummer", "Case_Number", "Autonummer", read_only=True),
    HubCaseField(
        "status",
        "Status",
        "Status",
        "Auswahlliste",
        required=True,
        options=("Neu", "Abgeschlossen", "Schwebend"),
    ),
    HubCaseField(
        "case_reason",
        "Fall-Grund",
        "Case_Reason",
        "Auswahlliste",
        options=("-None-", "Bestehendes Problem", "Neues Anliegen / Problem", "Änderungswunsch"),
    ),
    HubCaseField(
        "case_origin",
        "Fall Ursprung",
        "Case_Origin",
        "Auswahlliste",
        required=True,
        options=("-None-", "E-Mail", "Email / Tel.", "Web", "Telefon"),
    ),
    HubCaseField("created_time", "Zeitpunkt der Erstellung", "Created_Time", "Datum und Uhrzeit"),
    HubCaseField("description", "Beschreibung", "Description", "Mehrzeiliger Text"),
    HubCaseField("customer_name", "Kunde-Name", "Account_Name", "Verknüpfung"),
    HubCaseField("duration_minutes", "Dauer des Falls in Minuten", "Dauer_des_Falls_in_Minuten", "Ganzzahl"),
    HubCaseField(
        "billed_amount_net",
        "Betrag in Rechnung gestellt (netto)",
        "Betrag_in_Rechnung_gestellt_netto",
        "Ganzzahl",
    ),
)
