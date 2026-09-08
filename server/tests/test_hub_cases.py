import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_case import HubCase
from app.services.hub_cases import HubCaseError, HubCaseService


def _service(db: Session) -> HubCaseService:
    return HubCaseService(db=db, cipher=SecretCipher("a" * 32))


def _submitted_values(**overrides: str) -> dict[str, str]:
    values = {
        "case_field__status": "Neu",
        "case_field__case_reason": "Änderungswunsch",
        "case_field__case_origin": "E-Mail",
        "case_field__created_time": "2026-09-09T09:00",
        "case_field__description": "Der beschriebene Fall.",
        "case_field__duration_minutes": "45",
        "case_field__billed_amount_net": "250",
    }
    values.update(overrides)
    return values


def test_hub_case_is_numbered_linked_and_stored_encrypted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Fall Kunde", is_visible=True)
        db.add(customer)
        db.flush()

        case = _service(db).create_case(customer_id=customer.id, submitted_values=_submitted_values())
        db.commit()

        assert case.case_number == "FALL-000001"
        assert case.customer_id == customer.id
        assert "Der beschriebene Fall." not in case.encrypted_fields_json
        stored_values = json.loads(SecretCipher("a" * 32).decrypt(case.encrypted_fields_json))
        assert stored_values["status"] == "Neu"
        assert stored_values["duration_minutes"] == "45"

        detail = _service(db).get_detail(case_id=case.id)
        assert detail is not None
        assert detail.case_number == "FALL-000001"
        assert [(field.label, field.value) for field in detail.fields if field.key == "customer_name"] == [
            ("Kunde-Name", "Fall Kunde")
        ]
        assert [(field.label, field.value) for field in detail.fields if field.key == "created_time"] == [
            ("Zeitpunkt der Erstellung", "09.09.2026 09:00")
        ]


def test_hub_case_validates_required_choices_and_integer_fields():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = _service(db)
        with pytest.raises(HubCaseError, match="Fall Ursprung ist erforderlich"):
            service.create_case(customer_id=None, submitted_values=_submitted_values(**{"case_field__case_origin": "-None-"}))
        with pytest.raises(HubCaseError, match="Ganzzahl"):
            service.create_case(customer_id=None, submitted_values=_submitted_values(**{"case_field__duration_minutes": "eine Stunde"}))


def test_hub_case_update_replaces_fields_and_customer_link():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first_customer = Customer(name="Erster Kunde", is_visible=True)
        second_customer = Customer(name="Zweiter Kunde", is_visible=True)
        db.add_all([first_customer, second_customer])
        db.flush()
        service = _service(db)
        case = service.create_case(customer_id=first_customer.id, submitted_values=_submitted_values())
        db.commit()

        updated = service.update_case(
            case_id=case.id,
            customer_id=second_customer.id,
            submitted_values=_submitted_values(**{"case_field__status": "Abgeschlossen", "case_field__duration_minutes": "0"}),
        )
        db.commit()

        assert updated.customer_id == second_customer.id
        assert _service(db).list_cases()[0].status == "Abgeschlossen"
        assert db.get(HubCase, case.id) is not None
