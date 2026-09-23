from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes import accounts
from app.db.base import Base
from app.db.session import get_db
from app.models.hub_desktop_device import HubDesktopDevice
from app.models.hub_user import HubUser
from app.services.email_composer_settings import EmailComposerRuntimeSettings
from app.services.hub_accounts import HubAccountService
from app.services.styling_settings import StylingRuntimeSettings


ADMIN_SECTIONS = ("account-mcp", "account-users", "account-protocol", "account-openai",
    "account-provider-licenses", "account-access", "account-mailbox", "account-zoho")
ADMIN_ACTIONS = (
    ("/mcp-tokens", {"name": "Denied token"}),
    ("/mcp-tokens/1/revoke", {}),
    ("/integration-tokens", {"name": "Denied integration"}),
    ("/integration-tokens/1/revoke", {}),
    ("/openai", {"api_key": "not-a-real-api-key"}),
    ("/openai/model", {"model": "gpt-5.6-terra"}),
    ("/openai/remove", {}),
    ("/crocoblock", {"license_key": "not-a-real-license"}),
    ("/crocoblock/remove", {}),
    ("/provider-licenses", {"provider": "test", "license_key": "not-a-real-license"}),
    ("/provider-licenses/test/remove", {}),
)


def request(user, path="/account", query=b""):
    return Request({"type": "http", "method": "GET", "scheme": "https", "server": ("hub.test", 443),
        "path": path, "query_string": query, "headers": [],
        "state": {"hub_user": user}, "session": {"csrf_token": "test-csrf"}})


def assert_personal_only(html):
    assert 'id="account-security"' in html
    assert 'action="/account/password"' in html
    assert 'id="account-desktop-notifier"' in html
    assert 'action="/account/desktop-devices"' in html
    for section in ADMIN_SECTIONS:
        assert f'id="{section}"' not in html
        assert f'href="#{section}"' not in html
    for path, _ in ADMIN_ACTIONS:
        assert f'action="/account{path}"' not in html


@pytest.mark.parametrize("role", ["management", "employee", "viewer"])
def test_personal_context_never_loads_system_data_and_only_lists_own_devices(monkeypatch, role):
    def forbidden(*args, **kwargs):
        pytest.fail("Personal account must not load system data")

    for name in ("_zoho_service", "_zoho_books_service", "_legal_terms_context", "_pdf_template_context",
        "AiProviderConfigService", "ProviderCredentialService", "HubMailboxAccountService",
        "_administration", "list_activity_events"):
        monkeypatch.setattr(accounts, name, forbidden)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = HubUser(username="staff", password_hash="hash", role=role)
        other = HubUser(username="other", password_hash="hash", role="admin")
        db.add_all([user, other])
        db.flush()
        db.add_all([
            HubDesktopDevice(user_id=user.id, name="My PC", token_prefix="mine", token_digest="mine-digest"),
            HubDesktopDevice(user_id=other.id, name="Private admin PC", token_prefix="other", token_digest="other-digest"),
        ])
        db.flush()
        service = HubAccountService(db=db, app_secret_key="a" * 32)
        monkeypatch.setattr(service, "list_mcp_access_tokens", forbidden)
        monkeypatch.setattr(service, "list_integration_tokens", forbidden)
        req = request(user, query=b"openai=configured&zoho=connect-failed&user=updated")
        context = accounts._account_context(req, user, service)
        assert set(context) == {"page_mode", "user", "csrf_token", "desktop_devices",
            "new_desktop_device_token", "new_desktop_device_name", "error", "error_section"}
        assert [device.name for device in context["desktop_devices"]] == ["My PC"]
        html = accounts.templates.get_template("account.html").render(request=req, **context,
            styling=StylingRuntimeSettings(), email_composer_settings=EmailComposerRuntimeSettings(),
            email_ai_prompt_presets=(), agent_page_context=None)
        assert_personal_only(html)
        assert "My PC" in html and "Private admin PC" not in html
        assert "OpenAI is connected" not in html
        assert "Der Hub-Benutzer wurde gespeichert" not in html
        with pytest.raises(HTTPException) as exc:
            accounts._account_context(req, user, service, page_mode="settings")
        assert exc.value.status_code == 403
    engine.dispose()


@pytest.mark.parametrize("role", [None, "management", "viewer"])
@pytest.mark.parametrize("path,values", ADMIN_ACTIONS)
def test_direct_system_account_actions_fail_before_any_service_access(monkeypatch, role, path, values):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized action reached a service")

    for name in ("_account_service", "AiProviderConfigService", "ProviderCredentialService", "CrocoblockLicenseService"):
        monkeypatch.setattr(accounts, name, forbidden)
    app = FastAPI()
    app.include_router(accounts.router)
    app.dependency_overrides[get_db] = lambda: None

    @app.middleware("http")
    async def identity(req, call_next):
        req.state.hub_user = SimpleNamespace(id=2, role=role) if role else None
        req.scope["session"] = {"csrf_token": "test-csrf"}
        return await call_next(req)

    with TestClient(app) as client:
        response = client.post("/account" + path, data={"csrf_token": "test-csrf", **values})
    assert response.status_code == (403 if role else 401)


def test_personal_password_error_still_renders_without_admin_panels(monkeypatch):
    from app.core import templates

    user = SimpleNamespace(id=2, username="staff", display_name="Staff Member", role="management", is_active=True)

    def invalid_password(**kwargs):
        raise ValueError("Current password is incorrect.")

    service = SimpleNamespace(get_user=lambda user_id: user, change_password=invalid_password,
        list_desktop_devices=lambda **kwargs: [])
    monkeypatch.setattr(accounts, "_account_service", lambda db: service)
    monkeypatch.setattr(templates, "_module_access", lambda user: {})
    monkeypatch.setattr(templates, "_styling_settings", StylingRuntimeSettings)
    monkeypatch.setattr(templates, "_email_composer_settings", EmailComposerRuntimeSettings)
    monkeypatch.setattr(templates, "_email_ai_prompt_presets", lambda: ())
    response = accounts.change_password(request(user, "/account/password"), db=None,
        current_password="wrong", new_password="test-password", password_confirmation="test-password", csrf_token="test-csrf")
    assert response.status_code == 400
    html = response.body.decode()
    assert_personal_only(html)
    assert "Current password is incorrect." in html


def test_new_personal_device_pairing_context_does_not_drop_pairing_code():
    user = SimpleNamespace(role="management")
    service = SimpleNamespace(list_desktop_devices=lambda **kwargs: [])
    context = accounts._account_context(request(user, "/account/desktop-devices"), user, service,
        new_desktop_device_token="test-pairing-code", new_desktop_device_name="My PC")
    assert context["new_desktop_device_token"] == "test-pairing-code"
    assert context["new_desktop_device_name"] == "My PC"
    assert context["error_section"] == "account-desktop-notifier"
