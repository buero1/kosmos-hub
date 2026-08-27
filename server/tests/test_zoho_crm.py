import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.models.site import Site
from app.services.hub_accounts import hash_password
from app.services.zoho_crm import ZohoCrmService


def _user() -> HubUser:
    return HubUser(username="operator", password_hash=hash_password("correct-horse-battery-staple"), role="admin")


def _metadata() -> list[dict[str, str]]:
    return [
        {"api_name": "Account_Name", "field_label": "Kunde-Name"},
        {"api_name": "Phone", "field_label": "Tel."},
        {"api_name": "Website", "field_label": "Webseite"},
        {"api_name": "Customer_Number", "field_label": "Kunde-Nummer"},
        {"api_name": "Important_Info", "field_label": "Wichtige Infos"},
        {"api_name": "Contact_Email", "field_label": "Kontakt-E-Mail"},
    ]


def test_zoho_client_credentials_are_encrypted_and_require_a_new_connection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = _user()
        db.add(user)
        db.commit()

        service = ZohoCrmService(db=db, cipher=SecretCipher("a" * 32), public_base_url="https://hub.example")
        connection = service.configure(
            actor=user,
            data_center="eu",
            client_id="1000.client-id-for-kosmos",
            client_secret="client-secret-for-kosmos",
        )
        db.commit()

        assert connection.encrypted_client_id != "1000.client-id-for-kosmos"
        assert connection.encrypted_client_secret != "client-secret-for-kosmos"
        assert service.get_status().redirect_uri == "https://hub.example/account/zoho/callback"
        assert service.get_status().connected is False


def test_zoho_mapping_uses_field_labels_and_does_not_guess_duplicates():
    mapping = ZohoCrmService.resolve_account_field_mapping(
        _metadata()
        + [
            {"api_name": "Duplicate_Website", "field_label": "Webseite"},
            {"api_name": "Duration_Minutes", "field_label": "Dauer in Minuten"},
        ]
    )

    assert mapping["fields"]["customer_name"] == "Account_Name"
    assert mapping["fields"]["duration_minutes"] == "Duration_Minutes"
    assert mapping["fields"]["website"] is None
    assert mapping["fields"]["record_id"] == "id"


def test_zoho_sync_preserves_an_encrypted_customer_profile_without_linking_sites_automatically():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = _user()
        db.add_all(
            [
                user,
                Site(
                    uuid="1b0f66ae-4f6e-4c6f-8f59-2a49f92f1599",
                    domain="www.example-customer.de",
                    home_url="https://www.example-customer.de/",
                    site_url="https://www.example-customer.de/",
                ),
            ]
        )
        db.commit()

        cipher = SecretCipher("b" * 32)
        service = ZohoCrmService(db=db, cipher=cipher, public_base_url="https://hub.example")
        connection = service.configure(
            actor=user,
            data_center="eu",
            client_id="1000.client-id-for-kosmos",
            client_secret="client-secret-for-kosmos",
        )
        connection.encrypted_refresh_token = cipher.encrypt("zoho-refresh-token-for-test")
        db.commit()

        def fake_api_get(_connection, path, _params):
            if path.endswith("/settings/fields"):
                return {"fields": _metadata()}
            return {
                "data": [
                    {
                        "id": "4150868000001944196",
                        "Account_Name": "Example Customer GmbH",
                        "Website": "https://www.example-customer.de/",
                        "Customer_Number": "K-1001",
                        "Phone": "+49 89 123456",
                        "Important_Info": "Internal note",
                        "Contact_Email": "contact@example-customer.de",
                        "Modified_Time": "2026-08-27T10:15:00+02:00",
                    }
                ],
                "info": {"more_records": False},
            }

        service._api_get = fake_api_get
        result = service.sync_accounts()
        db.commit()

        customer = db.scalar(select(Customer).where(Customer.zoho_id == "4150868000001944196"))
        site = db.scalar(select(Site).where(Site.domain == "www.example-customer.de"))
        assert customer is not None
        assert customer.name == "Example Customer GmbH"
        assert customer.external_id == "K-1001"
        assert customer.website_domain == "example-customer.de"
        assert customer.encrypted_profile_json is not None
        assert "Internal note" not in customer.encrypted_profile_json
        assert json.loads(cipher.decrypt(customer.encrypted_profile_json))["fields"]["Wichtige Infos"] == "Internal note"
        assert result.created_customers == 1
        assert result.unique_site_match_candidates == 1
        assert site is not None and site.customer_id is None
