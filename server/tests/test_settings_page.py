from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes import accounts
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.hub_accounts import HubAccountService


SETTINGS_SECTIONS = (
    ("account-workflows", "Workflows"),
    ("account-legal-terms", "AGBs"),
    ("account-pdf-templates", "PDF-Vorlagen"),
    ("account-mailbox", "E-Mail-Postfächer"),
    ("account-zoho", "Zoho CRM"),
    ("account-zoho-books", "Zoho Books"),
    ("account-refresh-settings", "Refresh settings"),
    ("account-styling", "Styling"),
)

ACCOUNT_SECTIONS = (
    ("account-mcp", "MCP access"),
    ("account-desktop-notifier", "Windows Erinnerungen"),
    ("account-users", "Benutzer"),
    ("account-protocol", "Protokoll"),
    ("account-openai", "OpenAI"),
    ("account-provider-licenses", "Provider licenses"),
)


def test_settings_route_is_registered_without_the_account_prefix():
    assert "/settings" in {route.path for route in accounts.bootstrap_router.routes}


def test_account_template_separates_global_settings_from_account_sections():
    template = Path("app/templates/account.html").read_text(encoding="utf-8")
    assert 'account-session' not in template
    assert 'action="/account/logout"' not in template
    nav = template.split('<nav class="account-section-nav"', 1)[1].split("</nav>", 1)[0]
    settings_nav, account_nav = nav.split("{% else %}", 1)

    for section_id, label in SETTINGS_SECTIONS:
        assert f'href="#{section_id}"' in settings_nav
        assert f">{label}</a>" in settings_nav
        assert f'href="#{section_id}"' not in account_nav

    for section_id, label in ACCOUNT_SECTIONS:
        assert f'href="#{section_id}"' in account_nav
        assert f">{label}</a>" in account_nav
        assert f'href="#{section_id}"' not in settings_nav


def test_settings_contains_the_global_styling_form_and_legacy_hash_forwarding():
    template = Path("app/templates/account.html").read_text(encoding="utf-8")
    styling = Path("app/templates/partials/settings_styling.html").read_text(encoding="utf-8")

    assert '{% include "partials/settings_styling.html" %}' in template
    assert 'id="account-styling"' in styling
    assert 'action="/styling"' in styling
    assert 'window.location.replace(`/settings${window.location.search}${window.location.hash}`)' in template


def test_account_and_settings_modes_render_only_their_own_sections():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="kosmosadmin", password_hash="hash", role="admin")
        db.add(user)
        db.flush()
        service = HubAccountService(db=db, app_secret_key="a" * 32)
        template = accounts.templates.get_template("account.html")

        rendered = {}
        for mode, path in (("account", "/account"), ("settings", "/settings")):
            request = Request({
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "server": ("testserver", 80),
                "path": path,
                "query_string": b"",
                "headers": [],
                "session": {},
            })
            context = accounts._account_context(request, user, service, page_mode=mode)
            rendered[mode] = template.render(request=request, **context)

    assert 'id="account-users"' in rendered["account"]
    assert 'id="account-openai"' in rendered["account"]
    assert 'id="account-workflows"' not in rendered["account"]
    assert 'id="account-zoho"' not in rendered["account"]

    assert 'id="account-workflows"' in rendered["settings"]
    assert 'id="account-zoho"' in rendered["settings"]
    assert 'id="account-styling"' in rendered["settings"]
    assert 'id="account-users"' not in rendered["settings"]
    assert 'id="account-openai"' not in rendered["settings"]
