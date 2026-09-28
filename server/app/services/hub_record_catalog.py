"""Local record compatibility values; no external CRM connection or transport."""

from urllib.parse import urlsplit

# Imported identifiers and template module names remain stable for stored data.
ZOHO_ACCOUNT_MODULE = "Accounts"
ZOHO_CONTACT_MODULE = "Contacts"
ZOHO_RELEVANT_ACCOUNT_STATUSES = ("Aktuell", "Neu", "gekündigt", "Kündigung liegt vor")


class RecordDataError(ValueError):
    """A locally stored record cannot be used for the requested operation."""


def normalize_website_domain(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text if "://" in text else f"https://{text}")
        hostname = parsed.hostname.lower().strip(".") if parsed.hostname else ""
    except ValueError:
        return None
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or None
