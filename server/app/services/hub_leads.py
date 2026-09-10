"""Hub-only Lead persistence, validation and presentation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_lead import HubLead
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS, HUB_LEAD_SUBFORMS, HubLeadField, HubLeadSubform
from app.services.module_layouts import ModuleLayoutService


LEAD_FIELDS_LAYOUT_KEY = "lead-fields"


class HubLeadError(ValueError):
    """A safe validation message for the Leads UI."""


@dataclass(frozen=True)
class HubLeadFieldValue:
    key: str
    label: str
    display_type: str
    value: str
    form_value: str | tuple[str, ...] | bool
    required: bool = False
    read_only: bool = False
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class HubLeadSubformRow:
    fields: tuple[HubLeadFieldValue, ...]


@dataclass(frozen=True)
class HubLeadSubformValue:
    key: str
    label: str
    fields: tuple[HubLeadFieldValue, ...]
    rows: tuple[HubLeadSubformRow, ...]


@dataclass(frozen=True)
class HubLeadListEntry:
    lead: HubLead
    name: str
    company: str
    email: str
    status: str
    source: str
    created_time: str


@dataclass(frozen=True)
class HubLeadDetail:
    lead: HubLead
    name: str
    status: str
    fields: tuple[HubLeadFieldValue, ...]
    subforms: tuple[HubLeadSubformValue, ...]


class HubLeadService:
    """Keep Leads fully local until the later one-time Zoho import."""

    _MAX_FIELD_LENGTH = 20_000

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_leads(self) -> tuple[HubLeadListEntry, ...]:
        entries = [(lead, self._profile(lead)) for lead in self.db.scalars(select(HubLead)).all()]
        entries.sort(key=lambda item: self._sort_key(item[0], item[1]), reverse=True)
        return tuple(self._list_entry(lead, profile) for lead, profile in entries)

    def get_detail(self, *, lead_id: int) -> HubLeadDetail | None:
        lead = self.db.get(HubLead, lead_id)
        if lead is None:
            return None
        profile = self._profile(lead)
        values = self._fields(profile)
        default_keys = tuple(field.key for field in HUB_LEAD_FIELDS)
        ordered_keys = ModuleLayoutService(db=self.db).ordered_keys(
            layout_key=LEAD_FIELDS_LAYOUT_KEY,
            default_keys=default_keys,
        )
        by_key = {field.key: field for field in HUB_LEAD_FIELDS}
        fields = tuple(self._field_value(by_key[key], values.get(key)) for key in ordered_keys)
        return HubLeadDetail(
            lead=lead,
            name=self._name(values),
            status=self._display_option(by_key["lead_status"], values.get("lead_status")) or "-",
            fields=fields,
            subforms=self._subform_values(profile),
        )

    def new_form_values(self) -> dict[str, str | tuple[str, ...]]:
        return {"lead_field__lead_status": "Lead erstellt"}

    def create_lead(self, *, submitted_values: dict[str, object]) -> HubLead:
        profile = {
            "schema_version": 1,
            "source": "hub",
            "fields": self._submitted_fields(submitted_values, existing={}),
            "subforms": self._submitted_subforms(submitted_values, existing={}),
        }
        lead = HubLead(encrypted_profile_json=self._encrypt(profile))
        self.db.add(lead)
        self.db.flush()
        return lead

    def update_lead(self, *, lead_id: int, submitted_values: dict[str, object]) -> HubLead:
        lead = self.db.get(HubLead, lead_id)
        if lead is None:
            raise HubLeadError("Der Lead wurde nicht gefunden.")
        profile = self._profile(lead)
        profile["fields"] = self._submitted_fields(submitted_values, existing=self._fields(profile))
        profile["subforms"] = self._submitted_subforms(submitted_values, existing=self._subforms(profile))
        lead.encrypted_profile_json = self._encrypt(profile)
        self.db.flush()
        return lead

    def delete_lead(self, *, lead_id: int) -> HubLead:
        lead = self.db.get(HubLead, lead_id)
        if lead is None:
            raise HubLeadError("Der Lead wurde nicht gefunden.")
        self.db.delete(lead)
        self.db.flush()
        return lead

    def _list_entry(self, lead: HubLead, profile: dict[str, object]) -> HubLeadListEntry:
        values = self._fields(profile)
        by_key = {field.key: field for field in HUB_LEAD_FIELDS}
        return HubLeadListEntry(
            lead=lead,
            name=self._name(values),
            company=self._text(values.get("company")) or "-",
            email=self._text(values.get("email")) or "-",
            status=self._display_option(by_key["lead_status"], values.get("lead_status")) or "-",
            source=self._display_option(by_key["lead_source"], values.get("lead_source")) or "-",
            created_time=self._text(values.get("created_at_source")) or "-",
        )

    @staticmethod
    def _sort_key(lead: HubLead, profile: dict[str, object]) -> tuple[str, str, int]:
        values = HubLeadService._fields(profile)
        return (
            HubLeadService._text(values.get("created_at_source")),
            lead.created_at.isoformat() if lead.created_at is not None else "",
            lead.id,
        )

    def _field_value(self, definition: HubLeadField, raw_value: object) -> HubLeadFieldValue:
        if definition.display_type == "Mehrfachauswahl":
            selected = tuple(value for value in self._as_values(raw_value) if value)
            value = ", ".join(self._display_option(definition, item) or item for item in selected)
            form_value: str | tuple[str, ...] = selected
        elif definition.display_type == "Boolesch":
            form_value = bool(raw_value)
            value = "Ja" if form_value else "Nein"
        else:
            form_value = self._text(raw_value)
            value = self._display_option(definition, form_value) or form_value
        return HubLeadFieldValue(
            key=definition.key,
            label=definition.label,
            display_type=definition.display_type,
            value=value,
            form_value=form_value,
            required=definition.required,
            read_only=definition.read_only,
            options=definition.options,
        )

    def _subform_values(self, profile: dict[str, object]) -> tuple[HubLeadSubformValue, ...]:
        stored = self._subforms(profile)
        result: list[HubLeadSubformValue] = []
        for definition in HUB_LEAD_SUBFORMS:
            rows = stored.get(definition.key, [])
            visible_rows = tuple(
                HubLeadSubformRow(
                    fields=tuple(self._field_value(field, row.get(field.key)) for field in definition.fields)
                )
                for row in rows if isinstance(row, dict)
            )
            result.append(HubLeadSubformValue(
                key=definition.key,
                label=definition.label,
                fields=tuple(self._field_value(field, "") for field in definition.fields),
                rows=visible_rows,
            ))
        return tuple(result)

    def _submitted_fields(self, submitted_values: dict[str, object], *, existing: dict[str, object]) -> dict[str, object]:
        values: dict[str, object] = {}
        for definition in HUB_LEAD_FIELDS:
            if definition.read_only:
                values[definition.key] = existing.get(definition.key, "")
                continue
            raw_value = submitted_values.get(f"lead_field__{definition.key}")
            values[definition.key] = self._normalize_value(definition, raw_value, existing.get(definition.key))
        return values

    def _submitted_subforms(self, submitted_values: dict[str, object], *, existing: dict[str, list[dict[str, object]]]) -> dict[str, list[dict[str, object]]]:
        result: dict[str, list[dict[str, object]]] = {}
        for definition in HUB_LEAD_SUBFORMS:
            existing_rows = existing.get(definition.key, [])
            rows: list[dict[str, object]] = []
            for index, existing_row in enumerate(existing_rows):
                prefix = f"lead_subform__{definition.key}__{index}"
                if self._text(submitted_values.get(f"{prefix}__delete")).casefold() == "true":
                    continue
                row = {
                    field.key: (
                        existing_row.get(field.key, "") if field.read_only
                        else self._normalize_value(field, submitted_values.get(f"{prefix}__{field.key}"), existing_row.get(field.key))
                    )
                    for field in definition.fields
                }
                rows.append(row)
            new_prefix = f"lead_subform__{definition.key}__new"
            new_row = {
                field.key: self._normalize_value(field, submitted_values.get(f"{new_prefix}__{field.key}"), "")
                for field in definition.fields if not field.read_only
            }
            if any(self._has_value(value) for value in new_row.values()):
                rows.append(new_row)
            result[definition.key] = rows
        return result

    def _normalize_value(self, definition: HubLeadField, raw_value: object, existing: object) -> object:
        if definition.display_type == "Mehrfachauswahl":
            values = tuple(value.strip() for value in self._as_values(raw_value) if value.strip())
            self._validate_options(definition, values, existing)
            return list(values)
        value = self._text(raw_value).strip()
        if len(value) > self._MAX_FIELD_LENGTH:
            raise HubLeadError(f"{definition.label} ist zu lang.")
        if definition.display_type == "Boolesch":
            return value.casefold() in {"true", "1", "on", "yes"}
        self._validate_options(definition, (value,) if value else (), existing)
        return value

    @staticmethod
    def _validate_options(definition: HubLeadField, values: tuple[str, ...], existing: object) -> None:
        if not definition.options or not values:
            return
        allowed = {value for value, _label in definition.options}
        allowed.update(HubLeadService._as_values(existing))
        if any(value not in allowed for value in values):
            raise HubLeadError(f"Die Auswahl für {definition.label} ist ungültig.")

    @staticmethod
    def _display_option(definition: HubLeadField, value: object) -> str:
        text = HubLeadService._text(value)
        return next((label for option, label in definition.options if option == text), text)

    @staticmethod
    def _name(values: dict[str, object]) -> str:
        return " ".join(part for part in (HubLeadService._text(values.get("first_name")), HubLeadService._text(values.get("last_name"))) if part) or HubLeadService._text(values.get("company")) or "Ohne Name"

    def _profile(self, lead: HubLead) -> dict[str, object]:
        try:
            source = json.loads(self.cipher.decrypt(lead.encrypted_profile_json))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HubLeadError("Die gespeicherten Lead-Daten sind ungültig.") from exc
        return source if isinstance(source, dict) else {}

    def _encrypt(self, profile: dict[str, object]) -> str:
        return self.cipher.encrypt(json.dumps(profile, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _fields(profile: dict[str, object]) -> dict[str, object]:
        source = profile.get("fields")
        return source if isinstance(source, dict) else {}

    @staticmethod
    def _subforms(profile: dict[str, object]) -> dict[str, list[dict[str, object]]]:
        source = profile.get("subforms")
        if not isinstance(source, dict):
            return {}
        return {
            key: [row for row in rows if isinstance(row, dict)]
            for key, rows in source.items() if isinstance(key, str) and isinstance(rows, list)
        }

    @staticmethod
    def _text(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _as_values(value: object) -> tuple[str, ...]:
        if isinstance(value, (list, tuple)):
            return tuple(item for item in value if isinstance(item, str))
        return (value,) if isinstance(value, str) else ()

    @staticmethod
    def _has_value(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return any(item.strip() for item in HubLeadService._as_values(value))
