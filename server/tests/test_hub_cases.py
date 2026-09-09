import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_case import HubCase
from app.models.hub_user import HubUser
from app.services.hub_cases import CASE_FIELDS_LAYOUT_KEY, HubCaseError, HubCaseService
from app.services.module_layouts import ModuleLayoutService


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


def test_hub_case_detail_uses_the_saved_global_field_layout():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(admin)
        db.flush()
        case = _service(db).create_case(customer_id=None, submitted_values=_submitted_values())
        db.commit()

        original_detail = _service(db).get_detail(case_id=case.id)
        assert original_detail is not None
        original_keys = tuple(field.key for field in original_detail.fields)
        reordered_keys = ("description",) + tuple(key for key in original_keys if key != "description")
        ModuleLayoutService(db=db).configure(
            actor=admin,
            layout_key=CASE_FIELDS_LAYOUT_KEY,
            item_order_json=json.dumps(reordered_keys),
            allowed_keys=original_keys,
        )
        db.commit()

        detail = _service(db).get_detail(case_id=case.id)

        assert detail is not None
        assert tuple(field.key for field in detail.fields) == reordered_keys


def test_hub_case_delete_removes_only_the_hub_case():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        case = _service(db).create_case(customer_id=None, submitted_values=_submitted_values())
        case_id = case.id
        db.commit()

        deleted = _service(db).delete_case(case_id=case_id)
        db.commit()

        assert deleted.id == case_id
        assert db.get(HubCase, case_id) is None
