"""Import Zoho Cases into the reduced, Hub-native Fälle schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.hub_case import HubCase
from app.services.zoho_crm import ZohoCrmService


@dataclass(frozen=True)
class ZohoCaseImportResult:
    synchronized_cases: int
    created_cases: int
    updated_cases: int
    unlinked_cases: int
    number_collisions: int


class ZohoCaseImportService:
    """Keep Zoho case IDs stable while retaining every reviewed field locally."""

    _BERLIN = ZoneInfo("Europe/Berlin")

    def __init__(self, *, db: Session, cipher: SecretCipher, zoho_service: ZohoCrmService):
        self.db = db
        self.cipher = cipher
        self.zoho_service = zoho_service

    def synchronize_all_cases(self) -> ZohoCaseImportResult:
        return self.import_records(self.zoho_service.list_case_records())

    def import_records(self, records: list[dict[str, object]]) -> ZohoCaseImportResult:
        customers_by_zoho_id = {
            customer.zoho_id: customer
            for customer in self.db.scalars(select(Customer).where(Customer.zoho_id.is_not(None))).all()
            if customer.zoho_id
        }
        cases_by_zoho_id = {
            case.zoho_id: case
            for case in self.db.scalars(select(HubCase).where(HubCase.zoho_id.is_not(None))).all()
            if case.zoho_id
        }
        case_numbers = {
            case.case_number: case.zoho_id
            for case in self.db.scalars(select(HubCase).where(HubCase.case_number.is_not(None))).all()
            if case.case_number
        }
        synced_at = datetime.now(UTC)
        synchronized_cases = created_cases = updated_cases = unlinked_cases = number_collisions = 0

        for record in records:
            zoho_id = self._text(record.get("id"))
            if not zoho_id:
                continue
            synchronized_cases += 1
            account_id = self._lookup_id(record.get("Account_Name"))
            account_name = self._lookup_name(record.get("Account_Name"))
            customer = customers_by_zoho_id.get(account_id or "")
            if customer is None:
                unlinked_cases += 1

            case = cases_by_zoho_id.get(zoho_id)
            case_number = self._text(record.get("Case_Number"))
            number_is_available = not case_number or case_numbers.get(case_number) in {None, zoho_id}
            if case is None:
                case = HubCase(
                    zoho_id=zoho_id,
                    encrypted_fields_json=self.cipher.encrypt("{}"),
                )
                self.db.add(case)
                self.db.flush()
                created_cases += 1
                cases_by_zoho_id[zoho_id] = case
            else:
                updated_cases += 1

            if case_number and number_is_available:
                case.case_number = case_number
                case_numbers[case_number] = zoho_id
            elif case.case_number is None:
                case.case_number = f"FALL-{case.id:06d}"
                number_collisions += int(bool(case_number))

            case.customer = customer
            case.encrypted_fields_json = self.cipher.encrypt(
                json.dumps(
                    self._case_values(record, customer_name=customer.name if customer is not None else account_name),
                    ensure_ascii=False,
                )
            )
            case.zoho_modified_at = self._parse_datetime(record.get("Modified_Time"))
            case.zoho_synced_at = synced_at

        self.db.flush()
        return ZohoCaseImportResult(
            synchronized_cases=synchronized_cases,
            created_cases=created_cases,
            updated_cases=updated_cases,
            unlinked_cases=unlinked_cases,
            number_collisions=number_collisions,
        )

    @classmethod
    def _case_values(cls, record: dict[str, object], *, customer_name: str) -> dict[str, str]:
        return {
            "status": cls._text(record.get("Status")),
            "case_reason": cls._text(record.get("Case_Reason")),
            "case_origin": cls._text(record.get("Case_Origin")),
            "created_time": cls._form_datetime(record.get("Created_Time")),
            "description": cls._text(record.get("Description")),
            "customer_name": customer_name,
            "duration_minutes": cls._integer_text(record.get("Dauer_des_Falls_in_Minuten")),
            "billed_amount_net": cls._integer_text(record.get("Betrag_in_Rechnung_gestellt_netto")),
        }

    @staticmethod
    def _text(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _lookup_id(cls, value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        return cls._text(value.get("id")) or None

    @classmethod
    def _lookup_name(cls, value: object) -> str:
        return cls._text(value.get("name")) if isinstance(value, dict) else ""

    @classmethod
    def _form_datetime(cls, value: object) -> str:
        parsed = cls._parse_datetime(value)
        return parsed.astimezone(cls._BERLIN).strftime("%Y-%m-%dT%H:%M") if parsed is not None else ""

    @classmethod
    def _parse_datetime(cls, value: object) -> datetime | None:
        text = cls._text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    @classmethod
    def _integer_text(cls, value: object) -> str:
        text = cls._text(value)
        if not text:
            return ""
        try:
            decimal = Decimal(text)
        except InvalidOperation:
            return text
        return str(int(decimal)) if decimal == decimal.to_integral_value() else text
