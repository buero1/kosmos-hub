from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes import accounts
from app.core import templates
from app.services.styling_settings import StylingRuntimeSettings


def request(path="/account/login", query=b"", user=None):
    return Request({"type": "http", "method": "GET", "scheme": "https",
        "server": ("hub.test", 443), "path": path, "query_string": query, "headers": [],
        "session": {"csrf_token": "test-csrf"}, "state": {"hub_user": user}})


@pytest.fixture(autouse=True)
def no_hub_services_on_public_auth_pages(monkeypatch):
    monkeypatch.setattr(templates, "_styling_settings", StylingRuntimeSettings)

    def unexpected(*args, **kwargs):
        pytest.fail("Public auth pages must not load Hub data or editor/agent settings")

    for name in ("_module_access", "_unread_email_count", "_email_composer_settings",
                 "_email_ai_prompt_presets", "_agent_page_context"):
        monkeypatch.setattr(templates, name, unexpected)


def assert_auth_only(response):
    html = response.body.decode()
    for marker in ("app-sidebar", "app-navigation", "hub-quick-access", "agent-launcher",
                   "data-agent-float", "data-email-compose", "form-submit.js", "cdn.jsdelivr.net"):
        assert marker not in html
    assert '<main class="auth-shell">' in html
    assert '<section class="auth-card" aria-labelledby="auth-title">' in html
    assert 'name="csrf_token" value="test-csrf"' in html
    assert '--accent: ' + StylingRuntimeSettings().accent_color in html
    return html


def test_login_page_only_contains_standalone_form_and_preserves_next():
    response = accounts.login_page(request(), next="/emails?folder=drafts")
    assert response.status_code == 200
    html = assert_auth_only(response)
    assert '<script' not in html
    assert 'action="/account/login"' in html
    assert 'name="next" value="/emails?folder=drafts"' in html
    assert 'autocomplete="username"' in html
    assert 'autocomplete="current-password"' in html
    assert '>Benutzername<' in html and '>Passwort<' in html


def test_login_setup_success_and_initial_setup_use_auth_layout():
    html = assert_auth_only(accounts.login_page(request(query=b"setup=complete")))
    assert 'role="status"' in html and 'Administratorkonto wurde angelegt' in html
    setup = assert_auth_only(accounts.setup_page(request('/account/setup')))
    assert 'action="/account/setup"' in setup
    assert 'name="setup_token"' in setup
    assert 'window.location.hash' in setup


def test_failed_login_returns_only_auth_form_and_generic_error(monkeypatch):
    monkeypatch.setattr(accounts, '_account_service', lambda db: SimpleNamespace(authenticate=lambda *args: None))
    response = accounts.login(request(), db=None, username='unknown-user', password='wrong-password',
        csrf_token='test-csrf', next='/customers')
    assert response.status_code == 400
    html = assert_auth_only(response)
    assert 'Benutzername oder Passwort ist falsch.' in html
    assert 'role="alert"' in html
    assert 'wrong-password' not in html
    assert 'name="next" value="/customers"' in html


@pytest.mark.parametrize('next_url,expected', [('/emails?folder=drafts', '/emails?folder=drafts'),
    ('https://external.test', '/'), ('//external.test', '/')])
def test_successful_login_keeps_session_and_redirect_contract(monkeypatch, next_url, expected):
    user = SimpleNamespace(id=42, username='staff', session_version=2)
    monkeypatch.setattr(accounts, '_account_service', lambda db: SimpleNamespace(authenticate=lambda *args: user))
    monkeypatch.setattr(accounts, 'write_audit_log', lambda *args, **kwargs: None)
    req = request()
    response = accounts.login(req, db=SimpleNamespace(commit=lambda: None), username='staff', password='test',
        csrf_token='test-csrf', next=next_url)
    assert response.status_code == 303 and response.headers['location'] == expected
    assert req.session == {'user_id': 42, 'session_version': 2}
    assert accounts.login_page(request(user=user), next=next_url).headers['location'] == expected


def test_missing_csrf_does_not_attempt_authentication(monkeypatch):
    monkeypatch.setattr(accounts, '_account_service', lambda db: pytest.fail('Must reject CSRF first'))
    with pytest.raises(HTTPException) as exc:
        accounts.login(request(), db=None, username='staff', password='test', csrf_token='wrong')
    assert exc.value.status_code == 403


def test_error_text_is_escaped():
    html = accounts.templates.get_template('account_login.html').render(request=request(), next='',
        csrf_token='test-csrf', styling=StylingRuntimeSettings(), error='<script>attack()</script>')
    assert '<script>attack()</script>' not in html
    assert '&lt;script&gt;attack()&lt;/script&gt;' in html
