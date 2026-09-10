import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS, HUB_LEAD_SUBFORMS
from app.services.hub_leads import LEAD_FIELDS_LAYOUT_KEY, HubLeadService
from app.services.module_layouts import ModuleLayoutService


def _service(db: Session) -> HubLeadService:
    return HubLeadService(db=db, cipher=SecretCipher("a" * 32))


def _submitted_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "lead_field__first_name": "Erika",
        "lead_field__last_name": "Beispiel",
        "lead_field__company": "Beispiel GmbH",
        "lead_field__email": "erika@beispiel.de",
        "lead_field__industry": "Bäckereien",
        "lead_field__lead_status": "Lead erstellt",
        "lead_field__lead_source": "Web Research",
        "lead_field__email_status": ["Geöffnet", "Angeklickt"],
        "lead_field__created_at_source": "2026-09-10T09:00",
    }
    values.update(overrides)
    return values


def test_lead_catalog_contains_reviewed_fields_options_and_repeater():
    assert len(HUB_LEAD_FIELDS) == 52
    assert len(HUB_LEAD_SUBFORMS) == 1
    assert len(HUB_LEAD_SUBFORMS[0].fields) == 10
    industry = next(field for field in HUB_LEAD_FIELDS if field.key == "industry")
    assert ("Bäckereien", "Bäckereien") in industry.options
    assert len(industry.options) > 400


def test_hub_lead_stores_encrypted_fields_and_a_new_repeater_row():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        lead = _service(db).create_lead(
            submitted_values=_submitted_values(
                **{
                    "lead_subform__lead_results__new__billing_result_date": "2026-09-11",
                    "lead_subform__lead_results__new__lead_type": "Option 1",
                    "lead_subform__lead_results__new__lead_result": "Option 1",
                }
            )
        )
        db.commit()

        assert "Erika" not in lead.encrypted_profile_json
        stored = json.loads(SecretCipher("a" * 32).decrypt(lead.encrypted_profile_json))
        assert stored["fields"]["email_status"] == ["Geöffnet", "Angeklickt"]
        assert stored["subforms"]["lead_results"][0]["lead_type"] == "Option 1"

        detail = _service(db).get_detail(lead_id=lead.id)
        assert detail is not None
        assert detail.name == "Erika Beispiel"
        assert next(field.value for field in detail.fields if field.key == "lead_source") == "Internetrecherche"
        assert len(detail.subforms[0].rows) == 1
        assert next(field.value for field in detail.subforms[0].rows[0].fields if field.key == "lead_type") == "Termin vor Ort"


def test_hub_lead_does_not_save_an_empty_repeater_row_and_orders_newest_first():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        older = _service(db).create_lead(
            submitted_values=_submitted_values(**{"lead_field__created_at_source": "2026-09-09T09:00"})
        )
        newer = _service(db).create_lead(
            submitted_values=_submitted_values(
                **{
                    "lead_field__first_name": "Nora",
                    "lead_field__created_at_source": "2026-09-10T09:00",
                }
            )
        )
        db.commit()

        older_detail = _service(db).get_detail(lead_id=older.id)
        assert older_detail is not None
        assert older_detail.subforms[0].rows == ()
        assert [entry.lead.id for entry in _service(db).list_leads()] == [newer.id, older.id]


def test_hub_lead_detail_uses_the_saved_global_field_layout():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(admin)
        db.flush()
        lead = _service(db).create_lead(submitted_values=_submitted_values())
        db.commit()

        original_detail = _service(db).get_detail(lead_id=lead.id)
        assert original_detail is not None
        original_keys = tuple(field.key for field in original_detail.fields)
        reordered_keys = ("company",) + tuple(key for key in original_keys if key != "company")
        ModuleLayoutService(db=db).configure(
            actor=admin,
            layout_key=LEAD_FIELDS_LAYOUT_KEY,
            item_order_json=json.dumps(reordered_keys),
            allowed_keys=original_keys,
        )
        db.commit()

        detail = _service(db).get_detail(lead_id=lead.id)
        assert detail is not None
        assert tuple(field.key for field in detail.fields) == reordered_keys
