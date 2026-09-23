from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes import accounts
from app.core import templates
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.email_composer_settings import EmailComposerRuntimeSettings
from app.services.hub_access_control import HubAccessControlService, can_open_settings
from app.services.styling_settings import StylingRuntimeSettings


@pytest.fixture
def access(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = HubAccessControlService(db=db)
        service.ensure_defaults()
        monkeypatch.setattr(templates, "_module_access", lambda user: service.module_access(user) if user else {})
        monkeypatch.setattr(templates, "_styling_settings", StylingRuntimeSettings)
        monkeypatch.setattr(templates, "_email_composer_settings", EmailComposerRuntimeSettings)
        monkeypatch.setattr(templates, "_email_ai_prompt_presets", lambda: ())
        monkeypatch.setattr(templates, "_unread_email_count", lambda user: 0)
        yield service
    engine.dispose()


def request(path, user):
    return Request({"type": "http", "method": "GET", "scheme": "https",
        "server": ("hub.test", 443), "path": path, "query_string": b"", "headers": [],
        "session": {}, "state": {"hub_user": user}})


@pytest.mark.parametrize("role,active,settings_view,allowed", [
    ("management", True, False, False),
    ("management", True, True, False),
    ("viewer", True, False, False),
    ("admin", True, True, True),
    ("admin", False, True, False),
    (None, False, False, False),
])
@pytest.mark.parametrize("path", ["/emails", "/account", "/customers", "/calendar"])
@pytest.mark.parametrize("page_user_role", [None, "admin", "viewer"])
def test_settings_navigation_and_page_agree_with_actual_identity(
    access, monkeypatch, role, active, settings_view, allowed, path, page_user_role,
):
    user = None
    if role:
        user = HubUser(username="signed-in", password_hash="hash", role=role, is_active=active)
        access.db.add(user)
        access.db.flush()
        access.permission(role_key=role, module_key="settings").can_view = settings_view
        access.db.flush()

    req = request(path, user)
    context = templates._shared_template_context(req)
    assert context["can_open_settings"] is allowed
    if page_user_role:
        context["user"] = SimpleNamespace(role=page_user_role)
    rendered = templates.create_templates(directory="app/templates").get_template("base.html").render(
        request=req, **context,
    )
    navigation = rendered.split('<nav class="app-navigation"', 1)[1].split("</nav>", 1)[0]
    assert ('href="/settings"' in navigation) is allowed

    monkeypatch.setattr(accounts, "_account_service", lambda db: SimpleNamespace(get_user=lambda user_id: user))
    rendered_pages = []
    monkeypatch.setattr(accounts, "_account_context", lambda *args, **kwargs: {"page_mode": kwargs["page_mode"]})
    monkeypatch.setattr(accounts, "templates", SimpleNamespace(
        TemplateResponse=lambda req, name, context: rendered_pages.append(context["page_mode"]),
    ))
    if allowed:
        accounts.settings_page(request("/settings", user), db=access.db)
        assert rendered_pages == ["settings"]
    else:
        with pytest.raises(HTTPException) as exc:
            accounts.settings_page(request("/settings", user), db=access.db)
        assert exc.value.status_code == (403 if user else 401)
        assert not rendered_pages


def test_settings_policy_fails_closed_without_view_permission():
    user = SimpleNamespace(role="admin", is_active=True)
    assert not can_open_settings(user, can_view=False)
    assert not can_open_settings(None, can_view=True)


def test_template_without_shared_access_context_does_not_show_settings():
    rendered = templates.create_templates(directory="app/templates").get_template("base.html").render(
        request=request("/emails", None), user=SimpleNamespace(role="admin"),
        csrf_token="test-csrf", agent_page_context=None,
        styling=StylingRuntimeSettings(), email_composer_settings=EmailComposerRuntimeSettings(),
        email_ai_prompt_presets=(),
    )
    assert 'href="/settings"' not in rendered
