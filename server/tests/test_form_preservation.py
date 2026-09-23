import asyncio
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from app.core.form_responses import FormResponseMiddleware, redirect_error
from app.services.hub_finance_operations_shared import prefix_for
from app.services.hub_operations import get_operation
from tests.test_hub_finance_operations import env, read, values


@pytest.mark.parametrize("key", ["fields", "calendar", "activity", "access", "state", "email_state", "layout"])
def test_legacy_error_redirect_is_an_error_not_navigation(key):
    message_key = "message" if key == "state" else f"{key.removesuffix('_state')}_message"
    query = urlencode({key: "error", message_key: "Position: Einzelpreis netto ist erforderlich."})
    assert redirect_error(f"/example?{query}#fields") == "Position: Einzelpreis netto ist erforderlich."
    assert redirect_error("/example?search=error&fields=success") is None


@pytest.mark.parametrize("key", ["users", "direct_update", "plugin_install", "backup_action", "linked"])
def test_legacy_website_forms_keep_their_error_message(key):
    assert redirect_error(f"/sites/1?{key}=error&message=Missing+field") == "Missing field"


def test_opt_in_redirect_conversion_preserves_cookies_background_tasks_and_native_forms():
    app = FastAPI()
    app.add_middleware(FormResponseMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    jobs = []

    from fastapi import Request

    @app.post("/save")
    async def save(request: Request):
        request.session["saved"] = True
        return RedirectResponse("/saved#fields", status_code=303, background=BackgroundTask(jobs.append, "done"))

    @app.post("/invalid")
    async def invalid():
        return RedirectResponse("/record?fields=error&fields_message=Missing+price", status_code=303)

    with TestClient(app) as client:
        for path, expected, status in [("/save", {"redirect_url": "/saved#fields"}, 200),
                                       ("/invalid", {"detail": "Missing price"}, 400)]:
            response = client.post(path, headers={"X-Hub-Form": "preserve"}, follow_redirects=False)
            assert response.status_code == status
            assert response.json() == expected
            assert "location" not in response.headers
            assert response.headers["cache-control"] == "no-store"
        assert jobs == ["done"]
        assert client.cookies.get("session")
        assert client.post("/invalid", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("status", [307, 308])
def test_never_replays_post_redirects(status):
    app = FastAPI()
    app.add_middleware(FormResponseMiddleware)
    app.add_api_route("/save", lambda: RedirectResponse("/save/", status_code=status), methods=["POST"])
    with TestClient(app) as client:
        response = client.post("/save", headers={"X-Hub-Form": "preserve"})
    assert response.status_code == 400
    assert "redirect_url" not in response.json()


@pytest.mark.parametrize("status", [400, 401, 403, 422, 500])
def test_nonredirect_errors_keep_original_status_and_body(status):
    app = FastAPI()
    app.add_middleware(FormResponseMiddleware)
    app.add_api_route("/save", lambda: JSONResponse({"detail": "Validation failed"}, status_code=status), methods=["POST"])
    with TestClient(app) as client:
        response = client.post("/save", headers={"X-Hub-Form": "preserve"})
    assert response.status_code == status
    assert response.json() == {"detail": "Validation failed"}


@pytest.mark.parametrize("kind", ["offers", "orders", "invoices", "dunnings", "recurring-invoices"])
def test_finance_invalid_price_rolls_back_and_returns_error_then_all_edits_can_be_saved(env, kind, monkeypatch):
    from app.api.routes import web

    data = values(env, kind)
    result = env.service.execute(f"finance.{kind}.create", data)
    record_id = result.record_id
    env.db.commit()
    original = read(env, kind, record_id)
    prefix = prefix_for(kind)
    data = dict(get_operation(f"finance.{kind}.create").apply_defaults(data))
    data.update({f"{prefix}_line__0__name": "Changed website", f"{prefix}_line__0__unit_price": "",
                 f"{prefix}_line__1__name": "Changed support", f"{prefix}_line__1__unit_price": "250"})
    app = FastAPI()
    app.add_middleware(FormResponseMiddleware)
    if kind == "offers":
        path = f"/finance/offers/{record_id}/fields"
        app.add_api_route("/finance/offers/{offer_id}/fields", web.update_finance_offer_fields, methods=["POST"])
    else:
        path = f"/finance/{kind}/{record_id}/fields"
        app.add_api_route("/finance/{module_key}/{document_id}/fields", web.update_finance_document_fields, methods=["POST"])
    app.dependency_overrides[web.get_db] = lambda: env.db
    monkeypatch.setattr(web, "run_finance_pdf_generation", lambda *args: None)

    @app.middleware("http")
    async def user(request, call_next):
        request.state.hub_user = env.user
        return await call_next(request)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, data=data, headers={"X-Hub-Form": "preserve"})
            assert response.status_code == 400, response.text
            assert "Einzelpreis netto ist erforderlich" in response.json()["detail"]
            assert read(env, kind, record_id) == original
            assert "redirect_url" not in response.json()
            data[f"{prefix}_line__0__unit_price"] = "150"
            response = await client.post(path, data=data, headers={"X-Hub-Form": "preserve"})
            assert response.status_code == 200, response.text
            assert "success" in response.json()["redirect_url"]
            saved = read(env, kind, record_id)
            assert saved["lines"][0]["input_values"][f"{prefix}_line__0__name"] == "Changed website"
            assert saved["lines"][1]["input_values"][f"{prefix}_line__1__name"] == "Changed support"
            assert saved["lines"][1]["input_values"][f"{prefix}_line__1__unit_price"] == "250.00"

    asyncio.run(exercise())


def test_form_protection_is_wired_globally_not_per_module():
    from app.main import create_app
    assert any(item.cls is FormResponseMiddleware for item in create_app().user_middleware)
    base = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert '/static/form-submit.js?v=2' in base
