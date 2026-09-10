import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_lead import HubLead
from app.services.hub_leads import HubLeadService
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
