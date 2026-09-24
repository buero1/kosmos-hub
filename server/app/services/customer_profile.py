"""Resolve persisted customer labels to stable field identities for reads and writes."""

from dataclasses import dataclass

from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS


_CATALOG = {field.key: field for field in ZOHO_ACCOUNT_FIELDS if not field.subform_parent}
_SUBFORMS = {field.key for field in ZOHO_ACCOUNT_FIELDS if field.subform_parent}
_RETIRED = {"send_options_to_wordpress", "Options an WP senden", "Options_an_WP_senden"}


@dataclass(frozen=True)
class ResolvedCustomerField:
    key: str
    label: str
    value: object
    definition: dict[str, object]


def canonical_customer_metadata(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict):
        return {}
    return {
        key: {**definition, "label": _CATALOG[key].label}
        if key in _CATALOG and isinstance(definition, dict) else definition
        for key, definition in metadata.items()
    }


def resolve_customer_fields(profile: dict[str, object]) -> tuple[ResolvedCustomerField, ...]:
    values = profile.get("fields")
    if not isinstance(values, dict):
        return ()
    source = profile.get("field_metadata")
    metadata = {key: definition for key, definition in source.items()
                if isinstance(key, str) and isinstance(definition, dict)} if isinstance(source, dict) else {}
    aliases: dict[str, set[str]] = {}
    for key, definition in metadata.items():
        label = definition.get("label")
        if isinstance(label, str) and label:
            aliases.setdefault(label, set()).add(key)
    # Current catalog names/keys take precedence over stale display labels.
    for key, field in _CATALOG.items():
        aliases[key] = {key}
        aliases[field.label] = {key}

    resolved: dict[str, ResolvedCustomerField] = {}
    priorities: dict[str, int] = {}
    for stored_label, value in values.items():
        candidates = aliases.get(str(stored_label), set())
        key = next(iter(candidates)) if len(candidates) == 1 else str(stored_label)
        if key in _RETIRED or stored_label in _RETIRED:
            continue
        catalog = _CATALOG.get(key)
        if key in _SUBFORMS:
            continue
        label = catalog.label if catalog else str(stored_label)
        priority = 3 if stored_label == label else 2 if stored_label == key else 1
        if key in resolved and priorities[key] >= priority:
            continue
        definition = dict(metadata.get(key, {}))
        if catalog:
            definition.setdefault("display_type", catalog.display_type)
            definition["sensitive"] = catalog.sensitive or bool(definition.get("sensitive"))
        resolved[key] = ResolvedCustomerField(key, label, value, definition)
        priorities[key] = priority

    # Declared fields must keep their layout slot even when no value was stored.
    for key, definition in metadata.items():
        if key in _RETIRED or definition.get("label") in _RETIRED:
            continue
        if key in resolved or key in _SUBFORMS or definition.get("subform_parent"):
            continue
        catalog = _CATALOG.get(key)
        definition = dict(definition)
        if catalog:
            definition.setdefault("display_type", catalog.display_type)
            definition["sensitive"] = catalog.sensitive or bool(definition.get("sensitive"))
        label = catalog.label if catalog else str(definition.get("label") or key)
        resolved[key] = ResolvedCustomerField(key, label, None, definition)
    return tuple(resolved.values())
