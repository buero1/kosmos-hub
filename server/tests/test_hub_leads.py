import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS, HUB_LEAD_SUBFORMS
from app.services.hub_leads import LEAD_FIELDS_LAYOUT_KEY, HubLeadError, HubLeadService
from app.services.hub_workflows import (
    LEAD_APPOINTMENT_REMINDER_WORKFLOW_KEY,
    LEAD_RESULT_FIELD_UPDATE_WORKFLOW_KEY,
    HubWorkflowService,
)
from app.services.module_layouts import ModuleLayoutService


def _service(db: Session) -> HubLeadService:
    return HubLeadService(db=db, cipher=SecretCipher("a" * 32))


def _submitted_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "lead_field__first_name": "Erika",
        "lead_field__last_name": "Beispiel",
        "lead_field__salutation": "Frau Dr.",
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
    assert len(HUB_LEAD_FIELDS) == 53
    assert len(HUB_LEAD_SUBFORMS) == 1
    assert tuple(field.key for field in HUB_LEAD_SUBFORMS[0].fields) == (
        "lead_modified_by",
        "lead_modified_at",
        "billing_result_date",
        "lead_result",
        "lead_type",
        "order_date",
        "dialfire_follow_up_at",
        "created_at_source",
    )
    industry = next(field for field in HUB_LEAD_FIELDS if field.key == "industry")
    assert ("Bäckereien", "Bäckereien") in industry.options
    assert len(industry.options) > 400
    salutation = next(field for field in HUB_LEAD_FIELDS if field.key == "salutation")
    assert salutation.label == "Anrede"
    assert salutation.display_type == "Auswahlliste"
    assert tuple(value for value, _label in salutation.options) == ("Frau", "Herr", "Frau Dr.", "Herr Dr.")


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
        assert next(field.value for field in detail.fields if field.key == "salutation") == "Frau Dr."
        assert next(field.value for field in detail.fields if field.key == "lead_source") == "Internetrecherche"
        assert len(detail.subforms[0].rows) == 1
        assert next(field.value for field in detail.subforms[0].rows[0].fields if field.key == "lead_type") == "Termin vor Ort"


def test_lead_result_rows_are_sorted_by_modified_time_and_display_german_date_times():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = _service(db)
        lead = service.create_lead(
            submitted_values=_submitted_values(
                **{
                    "lead_subform__lead_results__new__lead_modified_by": "Alt",
                    "lead_subform__lead_results__new__lead_modified_at": "2026-07-20T11:55:00+02:00",
                }
            )
        )
        service.update_lead(
            lead_id=lead.id,
            submitted_values=_submitted_values(
                **{
                    "lead_subform__lead_results__0__lead_modified_by": "Alt",
                    "lead_subform__lead_results__0__lead_modified_at": "2026-07-20T11:55:00+02:00",
                    "lead_subform__lead_results__new__lead_modified_by": "Neu",
                    "lead_subform__lead_results__new__lead_modified_at": "2026-07-27T11:09:00+02:00",
                    "lead_subform__lead_results__new__dialfire_follow_up_at": "2026-07-28T08:30:00+02:00",
                    "lead_subform__lead_results__new__created_at_source": "2026-07-17T18:00:00+02:00",
                }
            ),
        )
        db.commit()

        detail = service.get_detail(lead_id=lead.id)
        assert detail is not None
        rows = detail.subforms[0].rows
        assert [next(field.value for field in row.fields if field.key == "lead_modified_by") for row in rows] == [
            "Neu",
            "Alt",
        ]
        newest_values = {field.key: field.value for field in rows[0].fields}
        assert newest_values["lead_modified_at"] == "27.07.2026 11:09"
        assert newest_values["dialfire_follow_up_at"] == "28.07.2026 08:30"
        assert newest_values["created_at_source"] == "17.07.2026 18:00"


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


def test_hub_lead_rejects_an_unknown_salutation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        with pytest.raises(HubLeadError, match="Anrede"):
            _service(db).create_lead(
                submitted_values=_submitted_values(**{"lead_field__salutation": "Professor"})
            )


@pytest.mark.parametrize(
    ("lead_result", "expected_status", "expected_billing_result"),
    (
        ("Stattgefunden + Auftrag", "Auftrag", "Stattgefunden und Auftrag"),
        ("Storniert", "Wertloses Lead", "Storno"),
        ("Zukünftig kontaktieren", "Zukünftig kontaktieren", ""),
        ("Kein Auftrag", "Verlorenes Lead", "Stattgefunden"),
        ("Stattgefunden und kein Auftrag", "Verlorenes Lead", "Stattgefunden"),
        ("Stattgefunden", "Kontaktiert", "Stattgefunden"),
        ("Vertrag", "Auftrag", "Auftrag"),
        ("Termin muss neugelegt werden", "Storno", "Storno"),
        ("Termin Beratungsgespräch", "Termin vereinbart", ""),
        ("Nicht mehr kontaktieren", "Nicht mehr kontaktieren", ""),
        ("Rücktritt", "Rücktritt", ""),
    ),
)
def test_lead_result_workflow_updates_the_configured_follow_up_fields(
    lead_result: str,
    expected_status: str,
    expected_billing_result: str,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = _service(db)
        lead = service.create_lead(
            submitted_values=_submitted_values(
                **{
                    "lead_field__lead_result": lead_result,
                    "lead_field__billing_result": "",
                }
            )
        )

        detail = service.get_detail(lead_id=lead.id)
        assert detail is not None
        values = {field.key: field.value for field in detail.fields}
        assert values["lead_status"] == expected_status
        assert values["billing_result"] == expected_billing_result


def test_lead_result_workflow_runs_only_for_a_change_and_can_be_disabled():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = _service(db)
        lead = service.create_lead(
            submitted_values=_submitted_values(**{"lead_field__lead_result": "Storniert"})
        )
        service.update_lead(
            lead_id=lead.id,
            submitted_values=_submitted_values(
                **{
                    "lead_field__lead_result": "Storniert",
                    "lead_field__lead_status": "Contacted",
                }
            ),
        )
        detail = service.get_detail(lead_id=lead.id)
        assert detail is not None
        assert next(field.value for field in detail.fields if field.key == "lead_status") == "Kontaktiert"

        workflow = next(
            workflow
            for workflow in HubWorkflowService(db=db).list_workflows()
            if workflow.workflow_key == LEAD_RESULT_FIELD_UPDATE_WORKFLOW_KEY
        )
        assert workflow.title == "Lead-Ergebnis Folgefelder"
        assert workflow.module_label == "Leads · Änderung Lead-Ergebnis"
        assert workflow.description == (
            "Setzt bei Änderungen am Lead-Ergebnis automatisch den passenden Lead-Status und, "
            "sofern vorgesehen, das Abrechnungsergebnis. Beim Wechsel auf \"Auftrag\" oder \"Stattgefunden + Auftrag\" werden "
            "bereits gesetzte Datumswerte beibehalten. Ein leeres Auftragsdatum erhält das Änderungsdatum "
            "in Berliner Zeit; ein leeres Abrechnungsergebnis-Datum übernimmt das Auftragsdatum. "
            "Bei unverändertem Lead-Ergebnis werden diese Datumsfelder nicht überschrieben."
        )
        workflow.is_enabled = False
        db.flush()

        service.update_lead(
            lead_id=lead.id,
            submitted_values=_submitted_values(
                **{
                    "lead_field__lead_result": "Stattgefunden",
                    "lead_field__lead_status": "Lead erstellt",
                    "lead_field__billing_result": "",
                }
            ),
        )
        detail = service.get_detail(lead_id=lead.id)
        assert detail is not None
        values = {field.key: field.value for field in detail.fields}
        assert values["lead_status"] == "Lead erstellt"
        assert values["billing_result"] == ""


def test_lead_result_workflow_also_runs_for_external_lead_updates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = _service(db)
        lead, created = service.upsert_external_lead(
            source_system="test-import",
            source_external_id="lead-123",
            field_values={
                "lead_result": "Stattgefunden",
                "lead_status": "Lead erstellt",
                "company": "External Example", "salutation": "Frau", "last_name": "Example",
            },
        )

        assert created is True
        detail = service.get_detail(lead_id=lead.id)
        assert detail is not None
        values = {field.key: field.value for field in detail.fields}
        assert values["lead_status"] == "Kontaktiert"
        assert values["billing_result"] == "Stattgefunden"

        same_lead, created = service.upsert_external_lead(
            source_system="test-import",
            source_external_id="lead-123",
            field_values={"lead_result": "Vertrag"},
        )
        assert created is False
        assert same_lead.id == lead.id
        detail = service.get_detail(lead_id=lead.id)
        assert detail is not None
        values = {field.key: field.value for field in detail.fields}
        assert values["lead_status"] == "Auftrag"
        assert values["billing_result"] == "Auftrag"


@pytest.mark.parametrize(
    ("hours_until_appointment", "lead_result", "initial_reminder", "expected_reminder"),
    (
        (61, "", False, True),
        (60, "", True, False),
        (1, "", True, False),
        (-1, "", True, False),
        (72, "Storniert", True, False),
        (72, "Termin muss neugelegt werden", True, False),
    ),
)
def test_lead_appointment_reminder_workflow_uses_the_60_hour_boundary_and_cancellations(
    hours_until_appointment: int,
    lead_result: str,
    initial_reminder: bool,
    expected_reminder: bool,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=2)))
    values: dict[str, object] = {
        "appointment_at": (now + timedelta(hours=hours_until_appointment)).isoformat(),
        "appointment_reminder": initial_reminder,
        "lead_result": lead_result,
    }

    with Session(engine) as db:
        HubWorkflowService(db=db).apply_lead_field_updates(
            previous_values={},
            updated_values=values,
            now=now,
        )

        assert values["appointment_reminder"] is expected_reminder


def test_lead_appointment_reminder_workflow_is_listed_and_can_be_disabled():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=2)))

    with Session(engine) as db:
        service = HubWorkflowService(db=db)
        workflow = next(
            workflow
            for workflow in service.list_workflows()
            if workflow.workflow_key == LEAD_APPOINTMENT_REMINDER_WORKFLOW_KEY
        )
        assert workflow.title == "Beratungstermin-Erinnerung"
        assert workflow.module_label == "Leads · Termindatum, Lead-Ergebnis"
        assert workflow.description == (
            "Setzt die Termin-Erinnerung bei Beratungsterminen mit mehr als 60 Stunden Vorlauf und "
            "entfernt sie bei kürzerem Vorlauf oder Stornierung."
        )
        workflow.is_enabled = False
        db.flush()
        values: dict[str, object] = {
            "appointment_at": (now + timedelta(hours=72)).isoformat(),
            "appointment_reminder": False,
            "lead_result": "",
        }

        service.apply_lead_field_updates(
            previous_values={},
            updated_values=values,
            now=now,
        )

        assert values["appointment_reminder"] is False


def test_hub_lead_save_applies_the_appointment_reminder_workflow():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    appointment = datetime.now(timezone.utc) + timedelta(hours=72)

    with Session(engine) as db:
        service = _service(db)
        lead = service.create_lead(
            submitted_values=_submitted_values(
                **{
                    "lead_field__appointment_at": appointment.isoformat(),
                    "lead_field__appointment_reminder": "",
                }
            )
        )
        detail = service.get_detail(lead_id=lead.id)
        assert detail is not None
        assert next(field.value for field in detail.fields if field.key == "appointment_reminder") == "Ja"

        service.update_lead(
            lead_id=lead.id,
            submitted_values=_submitted_values(
                **{
                    "lead_field__appointment_at": appointment.isoformat(),
                    "lead_field__appointment_reminder": "true",
                    "lead_field__lead_result": "Storniert",
                }
            ),
        )
        detail = service.get_detail(lead_id=lead.id)
        assert detail is not None
        assert next(field.value for field in detail.fields if field.key == "appointment_reminder") == "Nein"


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
