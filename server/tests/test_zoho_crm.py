import json
import uuid
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import SecretCipher, build_request_signature, calculate_body_sha256
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.models.site import Site, SiteStatus
from app.schemas.registration import RegistrationHeaders, RegistrationRequest
from app.services.hub_accounts import hash_password
from app.services.site_registration import SiteRegistrationService
from app.services.zoho_crm import ZOHO_CRM_SCOPES, ZohoCrmError, ZohoCrmService


def _user() -> HubUser:
    return HubUser(username="operator", password_hash=hash_password("correct-horse-battery-staple"), role="admin")


def _metadata() -> list[dict[str, str]]:
    return [
        {"api_name": "Account_Name", "field_label": "Kunde-Name"},
        {"api_name": "Account_Status", "field_label": "Status"},
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
        assert connection.scopes == ZOHO_CRM_SCOPES
        assert "ZohoCRM.modules.ALL" in connection.scopes
        assert "ZohoCRM.settings.ALL" in connection.scopes
        assert "ZohoCRM.send_mail.all.CREATE" in connection.scopes
        assert "ZohoCRM.share.all" in connection.scopes


def test_zoho_mapping_uses_field_labels_and_does_not_guess_duplicates():
    mapping = ZohoCrmService.resolve_account_field_mapping(
        _metadata()
        + [
            {"api_name": "Duplicate_Website", "field_label": "Webseite"},
            {"api_name": "Duration_Minutes", "field_label": "Dauer in Minuten"},
        ]
    )

    assert mapping["fields"]["customer_name"] == "Account_Name"
    assert mapping["fields"]["account_status"] == "Account_Status"
    assert mapping["fields"]["duration_minutes"] == "Duration_Minutes"
    assert mapping["fields"]["website"] is None
    assert mapping["fields"]["record_id"] == "id"


def test_zoho_sync_prepares_visible_customer_sites_for_bridge_onboarding():
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

        def fake_api_get(_connection, path, params, **_kwargs):
            if path.endswith("/settings/fields"):
                return {"fields": _metadata()}
            if path.endswith("/Accounts"):
                return {
                    "data": [
                        {
                            "id": "4150868000001944196",
                            "Account_Name": "Example Customer GmbH",
                            "Account_Status": "Aktuell",
                            "Website": "https://www.example-customer.de/",
                            "Customer_Number": "K-1001",
                            "Phone": "+49 89 123456",
                            "Important_Info": "Internal note",
                            "Contact_Email": "contact@example-customer.de",
                            "Modified_Time": "2026-08-27T10:15:00+02:00",
                        },
                        {
                            "id": "4150868000001944197",
                            "Account_Name": "Former Customer GmbH",
                            "Account_Status": "Archiviert",
                            "Website": "https://former-customer.de/",
                        },
                        {
                            "id": "4150868000001944198",
                            "Account_Name": "New Customer GmbH",
                            "Account_Status": "Neu",
                            "Website": "new-customer.de",
                        },
                    ],
                    "info": {"more_records": False},
                }
            return {
                "data": [],
                "info": {"more_records": False},
            }

        service._api_get = fake_api_get
        result = service.sync_accounts()
        db.commit()

        customer = db.scalar(select(Customer).where(Customer.zoho_id == "4150868000001944196"))
        hidden_customer = db.scalar(select(Customer).where(Customer.zoho_id == "4150868000001944197"))
        new_customer = db.scalar(select(Customer).where(Customer.zoho_id == "4150868000001944198"))
        site = db.scalar(select(Site).where(Site.domain == "www.example-customer.de"))
        prepared_site = db.scalar(select(Site).where(Site.domain == "new-customer.de"))
        assert customer is not None
        assert customer.name == "Example Customer GmbH"
        assert customer.external_id == "K-1001"
        assert customer.website_domain == "example-customer.de"
        assert customer.encrypted_profile_json is not None
        assert "Internal note" not in customer.encrypted_profile_json
        assert json.loads(cipher.decrypt(customer.encrypted_profile_json))["fields"]["Wichtige Infos"] == "Internal note"
        assert customer.zoho_status == "Aktuell"
        assert customer.is_visible is True
        assert hidden_customer is not None
        assert hidden_customer.zoho_status == "Archiviert"
        assert hidden_customer.is_visible is False
        assert new_customer is not None
        assert result.created_customers == 3
        assert result.synchronized_accounts == 3
        assert result.visible_accounts == 2
        assert result.hidden_customers == 1
        assert result.created_sites == 1
        assert result.linked_sites == 1
        assert result.site_conflicts == 0
        assert site is not None and site.customer_id == customer.id
        assert prepared_site is not None and prepared_site.customer_id == new_customer.id
        assert prepared_site.status == SiteStatus.pending.value
        assert prepared_site.connections == []


def test_zoho_sync_hides_existing_imports_missing_from_full_sync_without_unlinking_sites():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = _user()
        retained = Customer(name="Retained", zoho_id="zoho-current")
        obsolete = Customer(name="Obsolete", zoho_id="zoho-old", zoho_status="Aktuell", is_visible=True)
        linked_site = Site(
            uuid="7b0f66ae-4f6e-4c6f-8f59-2a49f92f1599",
            domain="obsolete.example",
            home_url="https://obsolete.example/",
            site_url="https://obsolete.example/",
            customer=obsolete,
        )
        db.add_all([user, retained, obsolete, linked_site])
        db.commit()

        cipher = SecretCipher("c" * 32)
        service = ZohoCrmService(db=db, cipher=cipher, public_base_url="https://hub.example")
        connection = service.configure(
            actor=user,
            data_center="eu",
            client_id="1000.client-id-for-kosmos",
            client_secret="client-secret-for-kosmos",
        )
        connection.encrypted_refresh_token = cipher.encrypt("zoho-refresh-token-for-test")

        def fake_api_get(_connection, path, params, **_kwargs):
            if path.endswith("/settings/fields"):
                return {"fields": _metadata()}
            if path.endswith("/Accounts"):
                return {
                    "data": [{"id": "zoho-current", "Account_Name": "Retained", "Account_Status": "Neu"}],
                    "info": {"more_records": False},
                }
            return {"data": [], "info": {"more_records": False}}

        service._api_get = fake_api_get
        result = service.sync_accounts()
        db.commit()

        assert result.synchronized_accounts == 1
        assert result.visible_accounts == 1
        assert result.hidden_customers == 1
        assert db.get(Customer, obsolete.id) is not None
        assert db.get(Customer, obsolete.id).zoho_status is None
        assert db.get(Customer, obsolete.id).is_visible is False
        assert db.get(Site, linked_site.id).customer_id == obsolete.id


def test_bridge_registration_adopts_one_preprovisioned_zoho_site():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Example Customer", zoho_id="zoho-1", zoho_status="Aktuell", is_visible=True)
        preprovisioned = Site(
            uuid=str(uuid.uuid4()),
            customer=customer,
            domain="example-customer.de",
            home_url="https://example-customer.de/",
            site_url="https://example-customer.de/",
            status=SiteStatus.pending.value,
        )
        db.add_all([customer, preprovisioned])
        db.commit()

        cipher = SecretCipher("d" * 32)
        bridge_uuid = str(uuid.uuid4())
        bridge_secret = "s" * 32
        registered_at = datetime.now(UTC)
        payload = RegistrationRequest(
            site_uuid=bridge_uuid,
            site_secret=bridge_secret,
            home_url="https://www.example-customer.de/",
            site_url="https://www.example-customer.de/",
            wordpress_version="6.9.1",
            php_version="8.3.0",
            bridge_version="0.3.51",
            mcp_endpoint="https://www.example-customer.de/wp-json/kosmos-bridge/v1",
            registration_timestamp=registered_at,
        )
        raw_body = b'{"registration":"test"}'
        timestamp = registered_at.isoformat()
        nonce = "bridge-registration-nonce"
        body_sha256 = calculate_body_sha256(raw_body)
        headers = RegistrationHeaders(
            site_uuid=bridge_uuid,
            timestamp=timestamp,
            nonce=nonce,
            body_sha256=body_sha256,
            signature=build_request_signature(bridge_uuid, timestamp, nonce, body_sha256, bridge_secret),
            request_id="test-request-id",
        )
        settings = Settings(app_secret_key="x" * 32, database_url="sqlite://")

        result = SiteRegistrationService(db=db, settings=settings, cipher=cipher).register(
            payload=payload,
            headers=headers,
            raw_body=raw_body,
        )

        sites = db.scalars(select(Site)).all()
        assert len(sites) == 1
        adopted = sites[0]
        assert result.site_id == preprovisioned.id
        assert result.message == "Pre-provisioned Zoho site connected."
        assert adopted.uuid == bridge_uuid
        assert adopted.domain == "www.example-customer.de"
        assert adopted.status == SiteStatus.verified.value
        assert adopted.customer_id == customer.id
        assert adopted.connections[0].endpoint == "https://www.example-customer.de/wp-json/kosmos-bridge/v1"


def test_zoho_request_surfaces_an_http_error_before_handling_empty_responses(monkeypatch):
    error = HTTPError(
        url="https://accounts.zoho.eu/oauth/v2/token",
        code=400,
        msg="Bad Request",
        hdrs=None,
        fp=BytesIO(b'{"error":"invalid_client"}'),
    )

    def raise_http_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("app.services.zoho_crm.urlopen", raise_http_error)

    with pytest.raises(ZohoCrmError, match="invalid_client"):
        ZohoCrmService._request_json(
            "https://accounts.zoho.eu/oauth/v2/token",
            method="POST",
            form={"grant_type": "authorization_code"},
            allow_empty_response=True,
        )
