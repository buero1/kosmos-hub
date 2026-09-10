from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_user import HubUser
from app.models.zoho_connection import ZohoConnection
from app.services.hub_accounts import hash_password
from app.services.zoho_books import ZOHO_BOOKS_SCOPES, ZohoBooksError, ZohoBooksService
from app.services.zoho_crm import ZohoCrmError


def _user() -> HubUser:
    return HubUser(username="books-admin", password_hash=hash_password("correct-horse-battery-staple"), role="admin")


def _crm_connection(*, cipher: SecretCipher, user: HubUser) -> ZohoConnection:
    return ZohoConnection(
        data_center="eu",
        encrypted_client_id=cipher.encrypt("1000.client-id-for-kosmos"),
        encrypted_client_secret=cipher.encrypt("client-secret-for-kosmos"),
        scopes="ZohoCRM.modules.ALL",
        configured_by_user_id=user.id,
    )


def test_books_reuses_the_configured_oauth_client_but_stores_a_separate_connection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("b" * 32)
        user = _user()
        db.add(user)
        db.flush()
        crm_connection = _crm_connection(cipher=cipher, user=user)
        db.add(crm_connection)
        db.commit()

        service = ZohoBooksService(db=db, cipher=cipher, public_base_url="https://hub.example")
        connection = service.prepare_authorization(actor=user)
        authorization_url = service.build_authorization_url(state="b" * 43)
        parsed = parse_qs(urlsplit(authorization_url).query)

        assert connection.id is not None
        assert connection.__class__ is not crm_connection.__class__
        assert connection.encrypted_client_id == crm_connection.encrypted_client_id
        assert connection.encrypted_client_secret == crm_connection.encrypted_client_secret
        assert connection.scopes == ZOHO_BOOKS_SCOPES
        assert parsed["scope"] == [ZOHO_BOOKS_SCOPES]
        assert parsed["redirect_uri"] == ["https://hub.example/account/zoho/callback"]
        assert parsed["access_type"] == ["offline"]
        assert service.get_status().connected is False
        assert service.get_status().crm_client_available is True


def test_books_authorization_selects_the_default_organization_and_uses_read_only_scopes(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("c" * 32)
        user = _user()
        db.add(user)
        db.flush()
        db.add(_crm_connection(cipher=cipher, user=user))
        db.commit()
        service = ZohoBooksService(db=db, cipher=cipher, public_base_url="https://hub.example")
        connection = service.prepare_authorization(actor=user)
        calls: list[tuple[str, str, dict[str, str] | None]] = []

        def fake_request_json(url, *, method, form=None, headers=None, **_kwargs):
            calls.append((url, method, form))
            if method == "POST" and form["grant_type"] == "authorization_code":
                return {"refresh_token": "books-refresh-token", "api_domain": "https://www.zohoapis.eu"}
            if method == "POST" and form["grant_type"] == "refresh_token":
                return {"access_token": "books-access-token", "api_domain": "https://www.zohoapis.eu"}
            assert method == "GET"
            assert headers == {"Authorization": "Zoho-oauthtoken books-access-token"}
            return {
                "organizations": [
                    {"organization_id": "101", "name": "Second Org", "is_default_org": False},
                    {"organization_id": "202", "name": "Default Org", "is_default_org": True},
                ]
            }

        monkeypatch.setattr(service, "_request_json", fake_request_json)
        organizations = service.complete_authorization(code="authorized-code-123")
        status = service.get_status()

        assert [item.id for item in organizations] == ["101", "202"]
        assert connection.encrypted_refresh_token != "books-refresh-token"
        assert status.organization_id == "202"
        assert status.organization_name == "Default Org"
        assert status.ready_for_import is True
        assert calls[0][2]["grant_type"] == "authorization_code"
        assert calls[1][2]["grant_type"] == "refresh_token"
        assert calls[2][:2] == ("https://www.zohoapis.eu/books/v3/organizations", "GET")


def test_books_requires_an_explicit_choice_when_no_default_organization_exists(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("d" * 32)
        user = _user()
        db.add(user)
        db.flush()
        db.add(_crm_connection(cipher=cipher, user=user))
        db.commit()
        service = ZohoBooksService(db=db, cipher=cipher, public_base_url="https://hub.example")
        service.prepare_authorization(actor=user)

        def fake_request_json(_url, *, method, form=None, **_kwargs):
            if method == "POST" and form["grant_type"] == "authorization_code":
                return {"refresh_token": "books-refresh-token", "api_domain": "https://www.zohoapis.eu"}
            if method == "POST":
                return {"access_token": "books-access-token", "api_domain": "https://www.zohoapis.eu"}
            return {
                "organizations": [
                    {"organization_id": "101", "name": "First Org", "is_default_org": False},
                    {"organization_id": "202", "name": "Second Org", "is_default_org": "false"},
                ]
            }

        monkeypatch.setattr(service, "_request_json", fake_request_json)
        service.complete_authorization(code="authorized-code-456")
        assert service.get_status().requires_organization_selection is True

        selected = service.select_organization(actor=user, organization_id="101")
        assert selected.organization_name == "First Org"
        assert service.get_status().ready_for_import is True


def test_books_translates_shared_zoho_request_errors(monkeypatch):
    def raise_crm_error(*_args, **_kwargs):
        raise ZohoCrmError("Zoho CRM is currently unreachable. Try again shortly.")

    monkeypatch.setattr("app.services.zoho_books.ZohoCrmService._request_json", raise_crm_error)

    with pytest.raises(ZohoBooksError, match="Zoho Books is currently unreachable"):
        ZohoBooksService._request_json("https://www.zohoapis.eu/books/v3/organizations", method="GET")
