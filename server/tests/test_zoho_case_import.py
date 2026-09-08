import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_case import HubCase
from app.services.zoho_case_import import ZohoCaseImportService


def _record(*, case_id: str, number: str, account_id: str, account_name: str, description: str, status: str = "Neu"):
    return {
        "id": case_id,
        "Case_Number": number,
        "Status": status,
        "Case_Reason": "Änderungswunsch",
        "Case_Origin": "E-Mail",
        "Created_Time": "2026-09-08T12:30:00+02:00",
        "Modified_Time": "2026-09-08T13:45:00+02:00",
        "Description": description,
        "Account_Name": {"id": account_id, "name": account_name},
        "Dauer_des_Falls_in_Minuten": "30",
        "Betrag_in_Rechnung_gestellt_netto": "120.00",
    }


def test_zoho_case_import_creates_updates_and_links_cases_without_deleting_manual_data():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(name="Verknüpfter Kunde", zoho_id="zoho-customer", is_visible=True)
        db.add(customer)
        db.flush()
        service = ZohoCaseImportService(db=db, cipher=cipher, zoho_service=None)  # type: ignore[arg-type]

        result = service.import_records(
            [
                _record(
                    case_id="zoho-case-1",
                    number="FALL-ZOHO-1",
                    account_id="zoho-customer",
                    account_name="Zoho Kunde",
                    description="Erster importierter Fall.",
                ),
                _record(
                    case_id="zoho-case-2",
                    number="FALL-ZOHO-2",
                    account_id="unbekannter-kunde",
                    account_name="Nur in Zoho",
                    description="Unverknüpfter Fall.",
                    status="Eskaliert",
                ),
            ]
        )
        db.commit()

        assert result.synchronized_cases == 2
        assert result.created_cases == 2
        assert result.updated_cases == 0
        assert result.unlinked_cases == 1
        first = db.query(HubCase).filter_by(zoho_id="zoho-case-1").one()
        second = db.query(HubCase).filter_by(zoho_id="zoho-case-2").one()
        assert first.case_number == "FALL-ZOHO-1"
        assert first.customer_id == customer.id
        assert "Erster importierter Fall." not in first.encrypted_fields_json
        assert json.loads(cipher.decrypt(first.encrypted_fields_json))["created_time"] == "2026-09-08T12:30"
        assert json.loads(cipher.decrypt(second.encrypted_fields_json))["customer_name"] == "Nur in Zoho"
        assert json.loads(cipher.decrypt(second.encrypted_fields_json))["status"] == "Eskaliert"

        update = service.import_records(
            [
                _record(
                    case_id="zoho-case-1",
                    number="FALL-ZOHO-1",
                    account_id="zoho-customer",
                    account_name="Zoho Kunde",
                    description="Aktualisierter importierter Fall.",
                    status="Abgeschlossen",
                )
            ]
        )
        db.commit()

        assert update.created_cases == 0
        assert update.updated_cases == 1
        assert db.query(HubCase).count() == 2
        assert json.loads(cipher.decrypt(first.encrypted_fields_json))["description"] == "Aktualisierter importierter Fall."
        assert json.loads(cipher.decrypt(first.encrypted_fields_json))["status"] == "Abgeschlossen"
