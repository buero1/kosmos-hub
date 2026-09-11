from types import SimpleNamespace
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
from app.services.zoho_crm import ZohoBinaryDownload, ZohoCrmError


def test_books_connect_persists_the_pending_connection_before_redirecting_to_zoho(monkeypatch):
    from app.api.routes import accounts

    prepared: list[HubUser] = []
    fake_service = SimpleNamespace(
        new_oauth_state=lambda: "b" * 43,
        prepare_authorization=lambda *, actor: prepared.append(actor),
        build_authorization_url=lambda *, state: f"https://accounts.zoho.com/oauth/v2/auth?state={state}",
        record_error=lambda _message: None,
    )
    commits: list[bool] = []
    monkeypatch.setattr(accounts, "_zoho_books_service", lambda _db: fake_service)
    user = SimpleNamespace(role="admin")
    request = SimpleNamespace(state=SimpleNamespace(hub_user=user), session={})
    db = SimpleNamespace(commit=lambda: commits.append(True))

    response = accounts.connect_zoho_books(request=request, db=db)

    assert prepared == [user]
    assert commits == [True]
    assert request.session["zoho_books_oauth_state"] == "b" * 43
    assert response.headers["location"] == f"https://accounts.zoho.com/oauth/v2/auth?state={'b' * 43}"


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
        # Zoho Books grants recurring-invoice reads through the invoice scope.
        assert "ZohoBooks.invoices.READ" in connection.scopes
        assert "ZohoBooks.recurringinvoices.READ" not in connection.scopes
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


def test_books_reuses_a_valid_access_token_for_multiple_api_reads(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("e" * 32)
        user = _user()
        db.add(user)
        db.flush()
        db.add(_crm_connection(cipher=cipher, user=user))
        db.commit()
        service = ZohoBooksService(db=db, cipher=cipher, public_base_url="https://hub.example")
        connection = service.prepare_authorization(actor=user)
        connection.encrypted_refresh_token = cipher.encrypt("books-refresh-token")
        refresh_calls: list[bool] = []

        def fake_request_json(_url, *, method, form=None, **_kwargs):
            if method == "POST" and form["grant_type"] == "refresh_token":
                refresh_calls.append(True)
                return {
                    "access_token": "books-access-token",
                    "api_domain": "https://www.zohoapis.eu",
                    "expires_in": 3600,
                }
            return {"organizations": []}

        monkeypatch.setattr(service, "_request_json", fake_request_json)

        service._api_get(connection, "/books/v3/organizations")
        service._api_get(connection, "/books/v3/organizations")

        assert refresh_calls == [True]


def test_books_reads_recent_invoice_ids_and_the_available_invoice_pdf(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("f" * 32)
        user = _user()
        db.add(user)
        db.flush()
        db.add(_crm_connection(cipher=cipher, user=user))
        db.commit()
        service = ZohoBooksService(db=db, cipher=cipher, public_base_url="https://hub.example")
        connection = service.prepare_authorization(actor=user)
        connection.encrypted_refresh_token = cipher.encrypt("books-refresh-token")
        connection.organization_id = "books-org-1"
        captured_paths: list[str] = []

        def fake_api_get(_connection, path):
            captured_paths.append(path)
            if path.startswith("/books/v3/invoices?"):
                return {"invoices": [{"invoice_id": "9001"}, {"invoice_id": "9002"}]}
            return {"invoice": {"invoice_id": "9001", "invoice_number": "RE-1"}}

        monkeypatch.setattr(service, "_api_get", fake_api_get)
        monkeypatch.setattr(
            service,
            "_api_get_binary",
            lambda _connection, path: (captured_paths.append(path) or ZohoBinaryDownload(b"%PDF-1.7", "application/pdf")),
        )

        assert service.list_recent_invoice_ids(limit=100) == ("9001", "9002")
        assert service.get_invoice(invoice_id="9001")["invoice_number"] == "RE-1"
        assert service.download_invoice_pdf(invoice_id="9001").content == b"%PDF-1.7"
        assert "organization_id=books-org-1" in captured_paths[0]
        assert "sort_column=date" in captured_paths[0]
        assert "accept=pdf" in captured_paths[-1]


def test_books_reads_all_invoice_ids_across_pages_without_duplicates(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("g" * 32)
        user = _user()
        db.add(user)
        db.flush()
        db.add(_crm_connection(cipher=cipher, user=user))
        db.commit()
        service = ZohoBooksService(db=db, cipher=cipher, public_base_url="https://hub.example")
        connection = service.prepare_authorization(actor=user)
        connection.encrypted_refresh_token = cipher.encrypt("books-refresh-token")
        connection.organization_id = "books-org-1"
        captured_paths: list[str] = []

        def fake_api_get(_connection, path):
            captured_paths.append(path)
            if "page=1" in path:
                return {
                    "invoices": [{"invoice_id": "9003"}, {"invoice_id": "9002"}],
                    "page_context": {"has_more_page": True},
                }
            return {
                "invoices": [{"invoice_id": "9002"}, {"invoice_id": "9001"}],
                "page_context": {"has_more_page": False},
            }

        monkeypatch.setattr(service, "_api_get", fake_api_get)

        assert service.list_all_invoice_ids() == ("9003", "9002", "9001")
        assert "page=1" in captured_paths[0]
        assert "page=2" in captured_paths[1]
        assert "per_page=200" in captured_paths[0]


def test_books_translates_shared_zoho_request_errors(monkeypatch):
    def raise_crm_error(*_args, **_kwargs):
        raise ZohoCrmError("Zoho CRM is currently unreachable. Try again shortly.")

    monkeypatch.setattr("app.services.zoho_books.ZohoCrmService._request_json", raise_crm_error)

    with pytest.raises(ZohoBooksError, match="Zoho Books is currently unreachable"):
        ZohoBooksService._request_json("https://www.zohoapis.eu/books/v3/organizations", method="GET")
