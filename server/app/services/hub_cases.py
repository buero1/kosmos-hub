"""Creation, validation and presentation of Hub-native cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.hub_case import HubCase
from app.services.hub_case_field_catalog import HUB_CASE_FIELDS, HubCaseField


class HubCaseError(ValueError):
    """A safe validation message for the Fälle UI."""


@dataclass(frozen=True)
class HubCaseFieldValue:
    key: str
    label: str
    display_type: str
    value: str
    form_value: str
    required: bool = False
    read_only: bool = False
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class HubCaseListEntry:
    case: HubCase
    case_number: str
    status: str
    customer_name: str
    case_origin: str
    created_time: str


@dataclass(frozen=True)
class HubCaseDetail:
    case: HubCase
    case_number: str
    fields: tuple[HubCaseFieldValue, ...]
    status: str


class HubCaseService:
    """Keep the reduced Fälle schema independent from Zoho CRM."""

    _BERLIN = ZoneInfo("Europe/Berlin")
    _MAX_LENGTHS = {
        "description": 20_000,
        "duration_minutes": 20,
        "billed_amount_net": 20,
        "status": 100,
        "case_reason": 255,
        "case_origin": 100,
    }

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_cases(self) -> tuple[HubCaseListEntry, ...]:
        cases = self.db.scalars(select(HubCase).order_by(HubCase.created_at.desc(), HubCase.id.desc())).all()
        return self._list_entries(cases)

    def list_cases_for_customer(self, *, customer_id: int) -> tuple[HubCaseListEntry, ...]:
        """Return only the cases linked to one Hub customer for its detail view."""
        cases = self.db.scalars(
            select(HubCase)
            .where(HubCase.customer_id == customer_id)
            .order_by(HubCase.created_at.desc(), HubCase.id.desc())
        ).all()
        return self._list_entries(cases)

    def _list_entries(self, cases: list[HubCase]) -> tuple[HubCaseListEntry, ...]:
        entries: list[HubCaseListEntry] = []
        for case in cases:
            values = self._values(case)
            entries.append(
                HubCaseListEntry(
                    case=case,
                    case_number=self.case_number(case),
                    status=values.get("status") or "-",
                    customer_name=case.customer.name if case.customer is not None else "-",
                    case_origin=self._selection_display(values.get("case_origin", "")),
                    created_time=self._display_datetime(values.get("created_time", "")),
                )
            )
        return tuple(entries)

    def list_linkable_customers(self) -> tuple[Customer, ...]:
        return tuple(
            self.db.scalars(
                select(Customer).where(Customer.is_visible.is_(True)).order_by(Customer.name.asc())
            ).all()
        )

    def get_detail(self, *, case_id: int) -> HubCaseDetail | None:
        case = self.db.get(HubCase, case_id)
        if case is None:
            return None
        values = self._values(case)
        fields = tuple(self._field_value(definition, case=case, values=values) for definition in HUB_CASE_FIELDS)
        return HubCaseDetail(
            case=case,
            case_number=self.case_number(case),
            fields=fields,
            status=values.get("status") or "-",
        )

    def new_form_values(self) -> dict[str, str]:
        return {
            "case_field__status": "Neu",
            "case_field__created_time": self._now_form_value(),
        }

    def create_case(self, *, customer_id: int | None, submitted_values: dict[str, str]) -> HubCase:
        customer = self._customer(customer_id)
        values = self._submitted_values(submitted_values, creating=True)
        if customer is not None:
            values["customer_name"] = customer.name
        case = HubCase(
            customer=customer,
            encrypted_fields_json=self._encrypt_values(values),
        )
        self.db.add(case)
        self.db.flush()
        case.case_number = f"FALL-{case.id:06d}"
        self.db.flush()
        return case

    def update_case(self, *, case_id: int, customer_id: int | None, submitted_values: dict[str, str]) -> HubCase:
        case = self.db.get(HubCase, case_id)
        if case is None:
            raise HubCaseError("Der Fall wurde nicht gefunden.")
        case.customer = self._customer(customer_id)
        existing_values = self._values(case)
        values = self._submitted_values(
            submitted_values,
            creating=False,
            allowed_legacy_values=existing_values,
        )
        if case.customer is not None:
            values["customer_name"] = case.customer.name
        elif existing_values.get("customer_name"):
            values["customer_name"] = existing_values["customer_name"]
        case.encrypted_fields_json = self._encrypt_values(values)
        self.db.flush()
        return case

    @staticmethod
    def case_number(case: HubCase) -> str:
        return case.case_number or f"FALL-{case.id:06d}"

    def _customer(self, customer_id: int | None) -> Customer | None:
        if customer_id is None:
            return None
        customer = self.db.get(Customer, customer_id)
        if customer is None or not customer.is_visible:
            raise HubCaseError("Der ausgewählte Kunde ist nicht verfügbar.")
        return customer

    def _submitted_values(
        self,
        submitted_values: dict[str, str],
        *,
        creating: bool,
        allowed_legacy_values: dict[str, str] | None = None,
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        for field in HUB_CASE_FIELDS:
            if field.key in {"case_number", "customer_name"}:
                continue
            value = str(submitted_values.get(f"case_field__{field.key}") or "").strip()
            if field.key == "created_time" and creating and not value:
                value = self._now_form_value()
            max_length = self._MAX_LENGTHS.get(field.key, 1_000)
            if len(value) > max_length:
                raise HubCaseError(f"{field.label} ist zu lang.")
            if field.required and (not value or value == "-None-"):
                raise HubCaseError(f"{field.label} ist erforderlich.")
            if (
                field.options
                and value
                and value not in field.options
                and value != (allowed_legacy_values or {}).get(field.key)
            ):
                raise HubCaseError(f"{field.label} enthält eine ungültige Auswahl.")
            if field.display_type == "Ganzzahl" and value:
                value = self._validated_integer(field, value)
            if field.display_type == "Datum und Uhrzeit" and value:
                value = self._validated_datetime(field, value)
            values[field.key] = value
        return values

    @staticmethod
    def _validated_integer(field: HubCaseField, value: str) -> str:
        try:
            number = int(value)
        except ValueError as exc:
            raise HubCaseError(f"{field.label} muss eine Ganzzahl sein.") from exc
        if number < 0:
            raise HubCaseError(f"{field.label} darf nicht negativ sein.")
        return str(number)

    @staticmethod
    def _validated_datetime(field: HubCaseField, value: str) -> str:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M").strftime("%Y-%m-%dT%H:%M")
        except ValueError as exc:
            raise HubCaseError(f"{field.label} enthält kein gültiges Datum mit Uhrzeit.") from exc

    def _field_value(
        self,
        field: HubCaseField,
        *,
        case: HubCase,
        values: dict[str, str],
    ) -> HubCaseFieldValue:
        if field.key == "case_number":
            raw_value = self.case_number(case)
            display_value = raw_value
        elif field.key == "customer_name":
            raw_value = str(case.customer_id or "")
            display_value = case.customer.name if case.customer is not None else values.get("customer_name", "")
        else:
            raw_value = values.get(field.key, "")
            display_value = raw_value
            if field.display_type == "Datum und Uhrzeit":
                display_value = self._display_datetime(raw_value)
            elif field.display_type == "Auswahlliste":
                display_value = self._selection_display(raw_value)
        return HubCaseFieldValue(
            key=field.key,
            label=field.label,
            display_type=field.display_type,
            value=display_value,
            form_value=raw_value,
            required=field.required,
            read_only=field.read_only,
            options=field.options,
        )

    def _values(self, case: HubCase) -> dict[str, str]:
        try:
            raw_values = json.loads(self.cipher.decrypt(case.encrypted_fields_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(raw_values, dict):
            return {}
        return {str(key): str(value or "") for key, value in raw_values.items()}

    def _encrypt_values(self, values: dict[str, str]) -> str:
        return self.cipher.encrypt(json.dumps(values, ensure_ascii=False))

    def _now_form_value(self) -> str:
        return datetime.now(self._BERLIN).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")

    @staticmethod
    def _selection_display(value: str) -> str:
        return "" if value == "-None-" else value

    @staticmethod
    def _display_datetime(value: str) -> str:
        if not value:
            return ""
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M").strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return value
