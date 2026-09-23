from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.templates import _agent_page_context, create_templates
from app.services.email_composer_settings import EmailComposerRuntimeSettings
from app.services.styling_settings import StylingRuntimeSettings


ADMINISTRATION_PATHS = (
    ("/sites", "Sites"),
    ("/users", "Users"),
    ("/backups", "Backups"),
    ("/updates", "Update workbench"),
    ("/plugin-installations", "Plugin installation"),
    ("/assistant", "Assistant"),
)

MAIN_NAVIGATION_LABELS = (
    "Dashboard",
    "E-Mails",
    "Customers",
    "Kontakte",
    "Leads",
    "Kalender",
    "Anrufe",
    "Aufgaben",
    "Meetings",
    "Fälle",
    "Finance",
    "Websites",
    "Hub-Agent",
    "Einstellungen",
    "Account",
)


def test_open_lead_page_is_selected_as_agent_context():
    request = SimpleNamespace(url=SimpleNamespace(path="/leads/1791"))
    assert _agent_page_context(request) == {"type": "lead", "key": "1791"}


def _render_base(path: str, *, admin: bool = False, signed_in_user=None, page_user=None) -> str:
    return create_templates(directory="app/templates").get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path=path), state=SimpleNamespace(hub_user=signed_in_user)),
        csrf_token="test-csrf-token",
        unread_email_count=0,
        email_composer_settings=EmailComposerRuntimeSettings(),
        email_ai_prompt_presets=(),
        styling=StylingRuntimeSettings(),
        can_use_global_email_composer=admin,
        can_open_settings=admin,
        agent_page_context=None,
        **({"user": page_user} if page_user is not None else {}),
    )


@pytest.mark.parametrize("role", ["admin", "management", "viewer"])
@pytest.mark.parametrize("path", ["/account", "/customers", "/emails", "/calendar"])
def test_sidebar_shows_current_identity_and_protected_logout_on_every_page(role, path):
    signed_in = SimpleNamespace(username="steffi", display_name="Stefanie Lier", role=role)
    edited = SimpleNamespace(username="other", display_name="Other employee", role="viewer")
    rendered = _render_base(path, signed_in_user=signed_in, page_user=edited)
    menu = rendered.split('<section class="app-user-menu"', 1)[1].split('</section>', 1)[0]
    assert '<strong>Stefanie Lier</strong>' in menu
    assert 'Angemeldet als steffi' in menu
    assert 'Other employee' not in menu
    assert 'href="/account"' in menu
    assert '<form method="post" action="/account/logout">' in menu
    assert 'name="csrf_token" value="test-csrf-token"' in menu
    assert 'Abmelden' in menu
    assert rendered.count('action="/account/logout"') == 1


def test_sidebar_identity_is_escaped_and_falls_back_to_username():
    user = SimpleNamespace(username="staff", display_name="", role="viewer")
    assert '<strong>staff</strong>' in _render_base('/account', signed_in_user=user)
    user.display_name = '<script>alert(1)</script>'
    rendered = _render_base('/account', signed_in_user=user)
    assert '<script>alert(1)</script>' not in rendered
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in rendered


def test_anonymous_pages_do_not_show_logout_or_another_users_identity():
    other = SimpleNamespace(username="other", display_name="Other employee", role="admin")
    rendered = _render_base('/account/login', page_user=other)
    assert '<section class="app-user-menu"' not in rendered
    assert 'action="/account/logout"' not in rendered


def test_administration_navigation_contains_the_requested_pages():
    template = Path("app/templates/base.html").read_text(encoding="utf-8")
    administration = template.split(">Websites</summary>", 1)[1].split("</details>", 1)[0]

    for path, label in ADMINISTRATION_PATHS:
        assert f'href="{path}"' in administration
        assert f">{label}</a>" in administration


@pytest.mark.parametrize(("path", "label"), ADMINISTRATION_PATHS)
def test_administration_navigation_is_open_and_marks_the_current_page(path: str, label: str):
    rendered = _render_base(path)

    assert '<details class="app-navigation-group" open>' in rendered
    assert '<summary class="app-navigation-group-summary is-active">Websites</summary>' in rendered
    assert f'<a href="{path}" class="is-active">{label}</a>' in rendered


def test_settings_replaces_the_standalone_styling_navigation_for_admins():
    template = Path("app/templates/base.html").read_text(encoding="utf-8")
    rendered = _render_base("/settings", admin=True)

    assert 'href="/settings"' in template
    assert '>Einstellungen</a>' in template
    assert 'href="/styling"' not in template
    assert '<a href="/settings" class="is-active">Einstellungen</a>' in rendered


def test_health_endpoint_is_not_exposed_as_a_sidebar_navigation_item():
    template = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert 'href="/healthz"' not in template
    assert ">Health</a>" not in template


def test_main_navigation_uses_the_requested_order():
    rendered = _render_base("/agent", admin=True)
    navigation = rendered.split('<nav class="app-navigation"', 1)[1].split("</nav>", 1)[0]

    positions = [navigation.index(f">{label}<") for label in MAIN_NAVIGATION_LABELS]
    assert positions == sorted(positions)


def test_main_navigation_links_and_group_summaries_use_the_accent_color():
    template = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert ".app-navigation a {\n        color: var(--accent);" in template
    assert ".app-navigation-group-summary {\n        display: flex;" in template
    summary_styles = template.split(".app-navigation-group-summary {", 1)[1].split("}", 1)[0]
    assert "color: var(--accent);" in summary_styles


def test_site_detail_navigation_uses_the_requested_order():
    template = Path("app/templates/site_detail.html").read_text(encoding="utf-8")
    navigation = template.split('<nav class="site-section-nav"', 1)[1].split("</nav>", 1)[0]
    labels = ("Dashboard", "Aktualisierungen", "Benutzer", "Backups", "Protokoll")

    positions = [navigation.index(f">{label}<") for label in labels]
    assert positions == sorted(positions)
