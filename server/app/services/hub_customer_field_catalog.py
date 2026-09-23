"""Field contract shared by the local customer form, validation and operations."""

from dataclasses import dataclass

from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS
from app.services.zoho_crm import ZOHO_RELEVANT_ACCOUNT_STATUSES


@dataclass(frozen=True)
class CustomerCreateField:
    key: str
    label: str
    required: bool = False
    max_length: int = 255
    input_type: str = "text"
    options: tuple[tuple[str, str], ...] = ()
    default: str = ""


HUB_CUSTOMER_FIELD_KEYS = (
    "customer_name", "account_status", "website", "phone", "industry", "billing_street",
    "billing_postal_code", "billing_city", "billing_state", "billing_country", "important_info",
)


def customer_create_fields() -> tuple[CustomerCreateField, ...]:
    definitions = {field.key: field for field in ZOHO_ACCOUNT_FIELDS}
    return tuple(CustomerCreateField(
        key=key, label={"customer_name": "Name", "account_status": "Status"}.get(key, definitions[key].label), required=key in {"customer_name", "account_status"},
        max_length=5000 if key == "important_info" else 255,
        input_type={"website": "url", "phone": "tel", "important_info": "textarea"}.get(key, "text"),
        options=tuple((status, status) for status in ZOHO_RELEVANT_ACCOUNT_STATUSES) if key == "account_status" else (),
        default="Neu" if key == "account_status" else "",
    ) for key in HUB_CUSTOMER_FIELD_KEYS)


def customer_create_defaults() -> dict[str, str]:
    return {f"customer_field__{field.key}": field.default for field in customer_create_fields() if field.default}
