import json
import uuid
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import SecretCipher, build_request_signature, calculate_body_sha256
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_user import HubUser
from app.models.site import Site, SiteStatus
from app.schemas.registration import RegistrationHeaders, RegistrationRequest
from app.services.hub_accounts import hash_password
from app.services.site_registration import SiteRegistrationService
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS
from app.services.zoho_crm import ZOHO_CRM_SCOPES, ZohoBinaryDownload, ZohoCrmError, ZohoCrmService


def test_zoho_callback_rejects_mismatched_state_without_an_internal_error(monkeypatch):
    from app.api.routes import accounts

    recorded_errors: list[str] = []
    fake_service = SimpleNamespace(record_error=recorded_errors.append)
    monkeypatch.setattr(accounts, "_zoho_service", lambda _db: fake_service)
    request = SimpleNamespace(
        state=SimpleNamespace(hub_user=SimpleNamespace(role="admin")),
        session={"zoho_oauth_state": "expected-state"},
    )

    response = accounts.zoho_callback(
        request=request,
        db=SimpleNamespace(commit=lambda: None),
        code="unused",
        state="different-state",
    )

    assert response.headers["location"] == "/account?zoho=connect-failed"
    assert recorded_errors == ["The Zoho connection state did not match. Start the connection again."]


def _user() -> HubUser:
    return HubUser(username="operator", password_hash=hash_password("correct-horse-battery-staple"), role="admin")


def _metadata() -> list[dict[str, str]]:
    return [
        {"api_name": "Account_Name", "field_label": "Kunde-Name"},
        {"api_name": "Account_Status", "field_label": "Status"},
        {"api_name": "Phone", "field_label": "Tel."},
        {"api_name": "Website", "field_label": "Webseite"},
        {"api_name": "Customer_Number", "field_label": "Kunde-Nummer"},
        {"api_name": "Customer_Type", "field_label": "Kunde Typ"},
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
        assert "ZohoCRM.modules.emails.READ" in connection.scopes
        assert "ZohoCRM.modules.notes.ALL" in connection.scopes
        assert "ZohoCRM.settings.ALL" in connection.scopes
        assert "ZohoCRM.settings.emails.READ" in connection.scopes
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
    assert mapping["fields"]["customer_type"] == "Customer_Type"
    assert mapping["fields"]["duration_minutes"] == "Duration_Minutes"
    assert mapping["fields"]["website"] == "Website"
    assert mapping["fields"]["record_id"] == "id"


def test_zoho_account_catalog_matches_the_reviewed_field_selection():
    root_fields = tuple(field for field in ZOHO_ACCOUNT_FIELDS if field.key != "record_id" and not field.subform_parent)
    from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_SUBFORMS

    assert len(root_fields) == 60
    assert sum(len(subform.fields) for subform in ZOHO_ACCOUNT_SUBFORMS) == 7


def test_zoho_field_metadata_keeps_picklist_options_and_respects_zoho_write_permissions():
    definitions = tuple(field for field in ZOHO_ACCOUNT_FIELDS if field.key in {"industry", "annual_cycle"})
    metadata = ZohoCrmService._field_metadata_by_key(
        definitions,
        [
            {
                "api_name": "Industry",
                "data_type": "picklist",
                "pick_list_values": [{"actual_value": "Handwerk", "display_value": "Handwerk"}],
            },
            {"api_name": "Jahresturnus", "data_type": "formula", "field_read_only": True},
        ],
        {"industry": "Industry", "annual_cycle": "Jahresturnus"},
    )

    assert metadata["industry"]["pick_list_values"] == [{"value": "Handwerk", "label": "Handwerk"}]
    assert metadata["industry"]["editable"] is True
    assert metadata["annual_cycle"]["editable"] is False


def test_zoho_field_changes_skip_empty_sensitive_values_and_validate_picklists():
    service = ZohoCrmService(db=SimpleNamespace(), cipher=SecretCipher("c" * 32), public_base_url="https://hub.example")
    profile = {"fields": {"Branche": "Handwerk", "IBAN": "DE02120300000000202051"}}
    metadata = {
        "industry": {
            "api_name": "Industry",
            "editable": True,
            "display_type": "Auswahlliste",
            "pick_list_values": [{"value": "Handwerk", "label": "Handwerk"}, {"value": "Beratung", "label": "Beratung"}],
        },
        "iban": {"api_name": "iban", "editable": True, "display_type": "Einzelzeile", "sensitive": True},
    }

    changes = service._root_field_changes(
        metadata,
        profile,
        {"customer_field__industry": "Beratung", "customer_field__iban": ""},
    )
    assert changes == {"Industry": "Beratung"}

    with pytest.raises(ZohoCrmError, match="selection list"):
        service._root_field_changes(metadata, profile, {"customer_field__industry": "Other"})


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
                            "Customer_Type": "Premium",
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
            if path.endswith("/Contacts"):
                return {
                    "data": [
                        {
                            "id": "4150868000003000001",
                            "Account_Name": {"id": "4150868000001944196", "name": "Example Customer GmbH"},
                            "Full_Name": "Anna Example",
                            "Email": "anna@example-customer.de",
                            "Secondary_Email": "anna.private@example-customer.de",
                            "Dritte_E_Mail_Adresse": "anna.other@example-customer.de",
                            "Phone": "+49 89 123456-1",
                            "Other_Phone": "+49 89 123456-2",
                            "Home_Phone": "+49 89 123456-3",
                            "Salutation": "Frau",
                            "Title": "Managing Director",
                            "Modified_Time": "2026-08-27T10:16:00+02:00",
                        },
                        {
                            "id": "4150868000003000002",
                            "Account_Name": {"id": "4150868000001944196", "name": "Example Customer GmbH"},
                            "First_Name": "Max",
                            "Last_Name": "Example",
                            "Mobile": "+49 170 1234567",
                            "Modified_Time": "2026-08-27T10:17:00+02:00",
                        },
                        {
                            "id": "4150868000003000003",
                            "Full_Name": "Unassigned Contact",
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
        assert json.loads(cipher.decrypt(customer.encrypted_profile_json))["fields"]["Kunde Typ"] == "Premium"
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
        assert result.synchronized_contacts == 3
        assert result.created_contacts == 2
        assert result.updated_contacts == 0
        assert result.removed_contacts == 0
        assert site is not None and site.customer_id == customer.id
        assert prepared_site is not None and prepared_site.customer_id == new_customer.id
        assert prepared_site.status == SiteStatus.pending.value
        assert prepared_site.connections == []
        contacts = list(db.scalars(select(CustomerContact).order_by(CustomerContact.zoho_id.asc())).all())
        assert [contact.customer_id for contact in contacts] == [customer.id, customer.id]
        assert "Anna Example" not in contacts[0].encrypted_profile_json
        contact_fields = json.loads(cipher.decrypt(contacts[0].encrypted_profile_json))["fields"]
        assert contact_fields["E-Mail"] == "anna@example-customer.de"
        assert contact_fields["Zweite E-Mail-Adresse"] == "anna.private@example-customer.de"
        assert contact_fields["Dritte E-Mail-Adresse"] == "anna.other@example-customer.de"
        assert contact_fields["Telefon alternativ"] == "+49 89 123456-2"
        assert contact_fields["Telefon privat"] == "+49 89 123456-3"
        assert contact_fields["Anrede"] == "Frau"


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


def test_zoho_reuses_one_access_token_until_it_is_near_expiry(monkeypatch):
    cipher = SecretCipher("e" * 32)
    service = ZohoCrmService(db=None, cipher=cipher, public_base_url="https://hub.example")  # type: ignore[arg-type]
    connection = type(
        "Connection",
        (),
        {
            "id": 999_999,
            "data_center": "eu",
            "encrypted_refresh_token": cipher.encrypt("zoho-refresh-token-for-test"),
            "encrypted_client_id": cipher.encrypt("1000.client-id-for-kosmos"),
            "encrypted_client_secret": cipher.encrypt("client-secret-for-kosmos"),
            "api_domain": None,
        },
    )()
    requested_urls: list[str] = []

    def fake_request_json(url, **_kwargs):
        requested_urls.append(url)
        return {"access_token": "cached-access-token", "api_domain": "https://www.zohoapis.eu", "expires_in_sec": 3600}

    monkeypatch.setattr(service, "_request_json", fake_request_json)

    assert service._refresh_access_token(connection) == "cached-access-token"
    assert service._refresh_access_token(connection) == "cached-access-token"
    assert requested_urls == ["https://accounts.zoho.eu/oauth/v2/token"]


def test_zoho_customer_communication_helpers_follow_notes_email_and_sender_api_shapes():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = _user()
        db.add(user)
        db.commit()
        cipher = SecretCipher("d" * 32)
        service = ZohoCrmService(db=db, cipher=cipher, public_base_url="https://hub.example")
        connection = service.configure(
            actor=user,
            data_center="eu",
            client_id="1000.client-id-for-kosmos",
            client_secret="client-secret-for-kosmos",
        )
        connection.encrypted_refresh_token = cipher.encrypt("zoho-refresh-token-for-test")

        def fake_api_get(_connection, path, params, **_kwargs):
            if path.endswith("/Notes"):
                assert params["page"] == "1"
                return {"data": [{"id": "zoho-note-1", "Note_Title": "Imported"}], "info": {"more_records": False}}
            if path.endswith("/Emails"):
                assert params == {}
                return {
                    "Emails": [{"message_id": "zoho-mail-1", "subject": "Imported mail"}],
                    "info": {"more_records": False},
                }
            if path.endswith("/Emails/zoho-mail-1"):
                assert params == {"user_id": "zoho-owner-1"}
                return {"data": [{"message_id": "zoho-mail-1", "content": "Full imported mail"}]}
            if path.endswith("from_addresses"):
                return {"from_addresses": [{"user_name": "Hub Team", "email": "team@example.de"}]}
            if path.endswith("email_templates"):
                assert params == {"per_page": "200", "page": "1"}
                return {
                    "email_templates": [{"id": "zoho-template-1", "name": "Statusvorlage", "subject": "Status"}],
                    "info": {"more_records": False},
                }
            if path.endswith("email_templates/zoho-template-1"):
                return {"email_templates": [{"id": "zoho-template-1", "name": "Statusvorlage", "subject": "Status", "content": "<p>Hallo</p>"}]}
            raise AssertionError(path)

        posted: list[tuple[str, dict[str, object]]] = []

        def fake_api_post(_connection, path, payload):
            posted.append((path, payload))
            if path.endswith("/Notes"):
                return {"data": [{"details": {"id": "zoho-note-created"}}]}
            return {"data": [{"details": {"message_id": "zoho-mail-created"}}]}

        service._api_get = fake_api_get
        service._api_post_json = fake_api_post

        assert service.list_account_notes("zoho-account-1") == [{"id": "zoho-note-1", "Note_Title": "Imported"}]
        assert service.list_record_email_headers("Accounts", "zoho-account-1") == [
            {"message_id": "zoho-mail-1", "subject": "Imported mail"}
        ]
        assert service.get_record_email(
            module="Accounts",
            record_id="zoho-account-1",
            message_id="zoho-mail-1",
            user_id="zoho-owner-1",
        ) == {"message_id": "zoho-mail-1", "content": "Full imported mail"}
        assert service.list_allowed_from_addresses() == [{"user_name": "Hub Team", "email": "team@example.de"}]
        assert service.list_email_templates() == [
            {"id": "zoho-template-1", "name": "Statusvorlage", "subject": "Status"}
        ]
        assert service.get_email_template(template_id="zoho-template-1") == {
            "id": "zoho-template-1", "name": "Statusvorlage", "subject": "Status", "content": "<p>Hallo</p>"
        }
        assert service.create_account_note(account_id="zoho-account-1", title="Hub note", content="Body") == {
            "id": "zoho-note-created"
        }
        assert service.send_account_email(
            account_id="zoho-account-1",
            sender_name="Hub Team",
            sender_email="team@example.de",
            recipient_name="Customer",
            recipient_email="customer@example.de",
            subject="Status",
            content="Plain text body",
            template_id="zoho-template-1",
        ) == {"message_id": "zoho-mail-created"}
        assert posted[1][1]["data"][0]["from"] == {"user_name": "Hub Team", "email": "team@example.de"}
        assert posted[1][1]["data"][0]["mail_format"] == "html"
        assert posted[1][1]["data"][0]["template"] == {"id": "zoho-template-1"}
        assert service.send_account_email(
            account_id="zoho-account-1",
            sender_name="Hub Team",
            sender_email="team@example.de",
            recipient_name="Customer",
            recipient_email="customer@example.de",
            subject="Re: Status",
            content="Antwort",
            reply_to_message_id="zoho-parent-message-1",
            reply_to_owner_id="zoho-owner-1",
        ) == {"message_id": "zoho-mail-created"}
        assert posted[2][1]["data"][0]["in_reply_to"] == {
            "message_id": "zoho-parent-message-1",
            "owner": {"id": "zoho-owner-1"},
        }
        assert service.send_account_email(
            account_id="zoho-account-1",
            sender_name="Hub Team",
            sender_email="team@example.de",
            recipient_name="Customer",
            recipient_email="customer@example.de",
            subject="Weiterleitung",
            content="Inhalt",
            cc_recipients=(("Office", "office@example.de"),),
            attachment_ids=("zfs-file-1",),
        ) == {"message_id": "zoho-mail-created"}
        assert posted[3][1]["data"][0]["cc"] == [{"user_name": "Office", "email": "office@example.de"}]
        assert posted[3][1]["data"][0]["attachments"] == [{"id": "zfs-file-1"}]

        downloaded_requests: list[tuple[str, dict[str, str]]] = []

        def fake_api_download(_connection, path, params):
            downloaded_requests.append((path, params))
            return ZohoBinaryDownload(content=b"%PDF-test", content_type="application/pdf")

        service._api_download = fake_api_download
        download = service.download_record_email_attachment(
            module="Accounts",
            record_id="zoho-account-1",
            message_id="zoho-mail-1",
            user_id="zoho-owner-1",
            attachment_id="zoho-attachment-1",
            filename="Angebot.pdf",
        )
        assert download.content == b"%PDF-test"
        assert downloaded_requests == [
            (
                "/crm/v8/Accounts/zoho-account-1/Emails/actions/download_attachments",
                {
                    "message_id": "zoho-mail-1",
                    "user_id": "zoho-owner-1",
                    "id": "zoho-attachment-1",
                    "name": "Angebot.pdf",
                },
            )
        ]

        inline_image = service.download_record_email_inline_image(
            module="Accounts",
            record_id="zoho-account-1",
            message_id="zoho-mail-1",
            user_id="zoho-owner-1",
            image_id="zoho-inline-image-1",
        )
        assert inline_image.content == b"%PDF-test"
        assert downloaded_requests[-1] == (
            "/crm/v8/Accounts/zoho-account-1/Emails/actions/download_inline_images",
            {
                "message_id": "zoho-mail-1",
                "user_id": "zoho-owner-1",
                "id": "zoho-inline-image-1",
            },
        )
