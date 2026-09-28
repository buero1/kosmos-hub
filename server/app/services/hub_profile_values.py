"""Local profile patches and shared date controls for imported and native records."""

from copy import deepcopy
from datetime import date, datetime
from uuid import uuid4

from app.core.timezones import BERLIN_TIMEZONE
from app.services.customer_profile import (
    resolve_customer_fields,
    canonical_customer_metadata,
)


DATE_TYPES = {"Datum", "date"}
DATETIME_TYPES = {"DatumZeit", "Datum und Uhrzeit", "datetime", "datetime-local"}


def normalize_date_value(value: object, display_type: str, label: str = "Datum") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        if display_type in DATE_TYPES:
            try:
                return date.fromisoformat(text).isoformat()
            except ValueError:
                return datetime.strptime(text, "%d.%m.%Y").date().isoformat()
        if display_type in DATETIME_TYPES:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.strptime(text, "%d.%m.%Y %H:%M")
            if "T" not in text and " " not in text:
                raise ValueError("Missing time")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=BERLIN_TIMEZONE)
                # Reject nonexistent local times in the spring DST transition.
                from datetime import UTC

                if parsed.astimezone(UTC).astimezone(BERLIN_TIMEZONE).replace(
                    tzinfo=None
                ) != parsed.replace(tzinfo=None):
                    raise ValueError("Nonexistent local time")
            return parsed.astimezone(BERLIN_TIMEZONE).isoformat(timespec="seconds")
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            f"{label}: Bitte ein gültiges Datum"
            + (" mit Uhrzeit" if display_type in DATETIME_TYPES else "")
            + " eingeben."
        ) from exc
    return text


def date_control_value(value: object, display_type: str) -> str:
    try:
        normalized = normalize_date_value(value, display_type)
    except ValueError:
        return str(value or "")
    if normalized and display_type in DATETIME_TYPES:
        return (
            datetime.fromisoformat(normalized)
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )
    return normalized


def profile_value(
    value: object, definition: dict, label: str, *, previous: object = None
) -> object:
    text = str(value or "").strip()
    if definition.get("sensitive") and not text:
        return previous
    if len(text) > 30000:
        raise ValueError(f"{label} ist zu lang.")
    display_type = str(definition.get("display_type") or "")
    if display_type == "Boolesch":
        return text.casefold() in {"true", "1", "on", "yes"}
    if display_type in DATE_TYPES | DATETIME_TYPES:
        return normalize_date_value(text, display_type, label) or None
    if display_type == "URL" and text:
        from urllib.parse import urlsplit

        try:
            url = urlsplit(text if "://" in text else "https://" + text)
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and "." in url.hostname
                and not any(c.isspace() for c in text)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{label}: Bitte eine gültige Website-Adresse angeben.")
    options = definition.get("pick_list_values") or []
    if options and text and text != str(previous or ""):
        allowed = {str(item.get("value")) for item in options if isinstance(item, dict)}
        if text not in allowed:
            raise ValueError(f"{label}: Bitte einen gültigen Wert auswählen.")
    return text or None


def patch_customer_profile(profile: dict, submitted: dict[str, str]) -> dict:
    if not isinstance(profile.get("fields"), dict):
        raise ValueError(
            "Die gespeicherten Kundendaten konnten nicht gelesen werden. Es wurde nichts überschrieben."
        )
    result = deepcopy(profile)
    resolved = resolve_customer_fields(profile)
    fields = {field.label: field.value for field in resolved}
    for field in resolved:
        key = f"customer_field__{field.key}"
        if key in submitted and field.definition.get("editable"):
            fields[field.label] = profile_value(
                submitted[key], field.definition, field.label, previous=field.value
            )
    result["fields"] = fields
    result["field_metadata"] = canonical_customer_metadata(result.get("field_metadata"))
    subforms = result.get("subforms")
    if not isinstance(subforms, dict):
        return result
    for key, subform in subforms.items():
        if not isinstance(subform, dict):
            continue
        metadata = subform.get("metadata", {})
        records = subform.get("records", [])
        if not isinstance(metadata, dict) or not isinstance(records, list):
            continue
        rows = []
        for index, original in enumerate(records):
            if not isinstance(original, dict) or not isinstance(
                original.get("values", {}), dict
            ):
                rows.append(original)
                continue
            prefix = f"customer_subform__{key}__{index}__"
            if submitted.get(prefix + "delete", "").lower() == "true":
                continue
            row = deepcopy(original)
            values = row.setdefault("values", {})
            for field, definition in metadata.items():
                if (
                    isinstance(definition, dict)
                    and definition.get("editable")
                    and prefix + field in submitted
                ):
                    values[field] = profile_value(
                        submitted[prefix + field],
                        definition,
                        definition.get("label", field),
                        previous=values.get(field),
                    )
            rows.append(row)
        prefix = f"customer_subform__{key}__new__"
        new_values = {
            field: profile_value(
                submitted[prefix + field], definition, definition.get("label", field)
            )
            for field, definition in metadata.items()
            if isinstance(definition, dict)
            and definition.get("editable")
            and prefix + field in submitted
        }
        if any(value not in (None, "", False) for value in new_values.values()):
            rows.append({"id": "hub-" + uuid4().hex, "values": new_values})
        subform["records"] = rows
    return result
