import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_lead import HubLead
from app.services.hub_leads import HubLeadService
from app.services.zoho_crm import ZohoCrmService
from app.services.zoho_lead_import import ZohoLeadImportService


def _record(*, lead_id: str, first_name: str, last_name: str) -> dict[str, object]:
    return {
        "id": lead_id,
        "First_Name": first_name,
        "Last_Name": last_name,
        "Company": "Beispiel GmbH",
        "Email": "kontakt@beispiel.de",
        "Industry": "Bäckereien",
        "Lead_Status": "Lead erstellt",
        "Lead_Source": "Web Research",
        "Email_Status": ["Geöffnet", "Angeklickt"],
        "Termin_Erinnerung_setzen": True,
        "Zeitpunkt_Leaderstellung": "2026-09-10T09:00:00+02:00",
        "Modified_Time": "2026-09-10T10:00:00+02:00",
        "_hub_lead_subforms": {
            "lead_results": [
                {
                    "Abrechnungsergebnis_Datum": "2026-09-11",
                    "Lead_Art": "Option 1",
                    "Lead_Ergebnis": "Option 1",
                    "Zeitpunkt_Leaderstellung": "2026-09-10T09:00:00+02:00",
                }
            ]
        },
    }


def test_zoho_lead_import_creates_and_updates_encrypted_hub_leads_without_deleting_manual_leads():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        manual_lead = HubLead(zoho_id=None, encrypted_profile_json=cipher.encrypt('{"source":"hub"}'))
        db.add(manual_lead)
        db.flush()
        service = ZohoLeadImportService(db=db, cipher=cipher, zoho_service=None)  # type: ignore[arg-type]

        result = service.import_records([_record(lead_id="zoho-lead-1", first_name="Erika", last_name="Beispiel")])
        db.commit()

        assert result.imported_leads == 1
        assert result.created_leads == 1
        assert result.updated_leads == 0
        assert result.imported_subform_rows == 1
        imported = db.query(HubLead).filter_by(zoho_id="zoho-lead-1").one()
        assert "Erika" not in imported.encrypted_profile_json
        stored = json.loads(cipher.decrypt(imported.encrypted_profile_json))
        assert stored["fields"]["email_status"] == ["Geöffnet", "Angeklickt"]
        assert stored["fields"]["appointment_reminder"] is True
        assert stored["subforms"]["lead_results"][0]["lead_type"] == "Option 1"
        assert db.get(HubLead, manual_lead.id) is not None

        detail = HubLeadService(db=db, cipher=cipher).get_detail(lead_id=imported.id)
        assert detail is not None
        assert detail.name == "Erika Beispiel"
        assert next(field.value for field in detail.fields if field.key == "lead_source") == "Internetrecherche"
        assert next(field.value for field in detail.fields if field.key == "appointment_reminder") == "Ja"
        assert next(field.value for field in detail.subforms[0].rows[0].fields if field.key == "lead_type") == "Termin vor Ort"

        update = service.import_records([_record(lead_id="zoho-lead-1", first_name="Erika", last_name="Aktualisiert")])
        db.commit()

        assert update.created_leads == 0
        assert update.updated_leads == 1
        assert db.query(HubLead).filter_by(zoho_id="zoho-lead-1").count() == 1
        updated_detail = HubLeadService(db=db, cipher=cipher).get_detail(lead_id=imported.id)
        assert updated_detail is not None
        assert updated_detail.name == "Erika Aktualisiert"


def test_lead_pagination_continues_with_zoho_page_token_after_2000_rows(monkeypatch):
    service = ZohoCrmService(db=None, cipher=None, public_base_url="https://hub.example")  # type: ignore[arg-type]
    responses = [
        {"data": [{"id": str(page)}], "info": {"more_records": True}}
        for page in range(1, 10)
    ]
    responses.append(
        {
            "data": [{"id": "10"}],
            "info": {"more_records": True, "next_page_token": "after-2000"},
        }
    )
    responses.append({"data": [{"id": "11"}], "info": {"more_records": False}})
    requests: list[dict[str, str]] = []

    def fake_api_get(_connection, _path, query, *, allow_empty_response):
        assert allow_empty_response is True
        requests.append(query)
        return responses.pop(0)

    monkeypatch.setattr(service, "_api_get", fake_api_get)

    pages = list(
        service._get_all_lead_module_pages(
            object(),
            module="Leads",
            requested_fields=["Last_Name"],
            label="Leads",
        )
    )

    assert [page["data"][0]["id"] for page in pages] == [str(number) for number in range(1, 12)]
    assert requests[-1]["page_token"] == "after-2000"
    assert "page" not in requests[-1]
