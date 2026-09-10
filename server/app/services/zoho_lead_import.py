"""One-time import of Zoho Leads into the Hub-only Leads module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_lead import HubLead
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS, HUB_LEAD_SUBFORMS, HubLeadField
from app.services.zoho_crm import ZohoCrmService


@dataclass(frozen=True)
class ZohoLeadImportResult:
    imported_leads: int
    created_leads: int
    updated_leads: int
    imported_subform_rows: int


class ZohoLeadImportService:
    """Store a complete, local Lead snapshot without enabling future synchronization."""

    _BERLIN = ZoneInfo("Europe/Berlin")

    def __init__(self, *, db: Session, cipher: SecretCipher, zoho_service: ZohoCrmService):
        self.db = db
        self.cipher = cipher
        self.zoho_service = zoho_service

    def import_all_leads(self) -> ZohoLeadImportResult:
        return self.import_records(self.zoho_service.list_lead_records())

    def import_records(self, records: list[dict[str, object]]) -> ZohoLeadImportResult:
        leads_by_zoho_id = {
            lead.zoho_id: lead
            for lead in self.db.scalars(select(HubLead).where(HubLead.zoho_id.is_not(None))).all()
            if lead.zoho_id
        }
        imported_at = datetime.now(UTC)
        imported_leads = created_leads = updated_leads = imported_subform_rows = 0

        for record in records:
            zoho_id = self._text(record.get("id"))
            if not zoho_id:
                continue
            imported_leads += 1
            lead = leads_by_zoho_id.get(zoho_id)
            if lead is None:
                lead = HubLead(zoho_id=zoho_id, encrypted_profile_json=self.cipher.encrypt("{}"))
                self.db.add(lead)
                leads_by_zoho_id[zoho_id] = lead
                created_leads += 1
            else:
                updated_leads += 1

            subforms = self._subform_values(record)
            imported_subform_rows += sum(len(rows) for rows in subforms.values())
            profile = {
                "schema_version": 1,
                "source": "zoho-import",
                "fields": {
                    field.key: self._value_for_field(field, record.get(field.api_name))
                    for field in HUB_LEAD_FIELDS
                },
                "subforms": subforms,
            }
            lead.encrypted_profile_json = self.cipher.encrypt(
                json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
            )
            lead.zoho_modified_at = self._parse_datetime(record.get("Modified_Time"))
            lead.zoho_imported_at = imported_at

        self.db.flush()
        return ZohoLeadImportResult(
            imported_leads=imported_leads,
            created_leads=created_leads,
            updated_leads=updated_leads,
            imported_subform_rows=imported_subform_rows,
        )

    def _subform_values(self, record: dict[str, object]) -> dict[str, list[dict[str, object]]]:
        raw_subforms = record.get("_hub_lead_subforms")
        source = raw_subforms if isinstance(raw_subforms, dict) else {}
        values: dict[str, list[dict[str, object]]] = {}
        for subform in HUB_LEAD_SUBFORMS:
            rows = source.get(subform.key)
            raw_rows = rows if isinstance(rows, list) else []
            values[subform.key] = [
                {
                    field.key: self._value_for_field(field, row.get(field.api_name))
                    for field in subform.fields
                }
                for row in raw_rows
                if isinstance(row, dict)
            ]
        return values

    def _value_for_field(self, field: HubLeadField, raw_value: object) -> object:
        if field.display_type == "Mehrfachauswahl":
            return [value for value in (self._text(item) for item in self._items(raw_value)) if value]
        if field.display_type == "Boolesch":
            return raw_value is True or self._text(raw_value).casefold() in {"true", "1", "yes"}
        value = self._text(raw_value)
        if field.display_type == "Datum":
            return self._form_date(value)
        if field.display_type == "DatumZeit":
            return self._form_datetime(value)
        return value

    @staticmethod
    def _items(value: object) -> tuple[object, ...]:
        return tuple(value) if isinstance(value, list) else (value,)

    @staticmethod
    def _text(value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("name", "actual_value", "display_value", "id"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    return text.strip()
            return ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return ""

    @classmethod
    def _form_date(cls, value: str) -> str:
        if not value:
            return ""
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return value

    @classmethod
    def _form_datetime(cls, value: str) -> str:
        parsed = cls._parse_datetime(value)
        return parsed.astimezone(cls._BERLIN).strftime("%Y-%m-%dT%H:%M") if parsed is not None else value

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        text = ZohoLeadImportService._text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
