from datetime import UTC, datetime
import json

import pytest
from fastapi import HTTPException
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_user import HubUser
from app.models.hub_wordpress_job import HubWordPressJob
from app.models.site import Site
from app.services.hub_operations import HubOperationError, HubOperationService, get_operation
from app.services.plugin_auto_updates import PluginAutoUpdateService as Policy
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService
from app.services.wordpress_jobs import process_next


@pytest.fixture
def setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    cipher = SecretCipher("auto-updates-test")
    with sessions() as db:
        db.add_all([HubUser(username="admin", role="admin", password_hash="hash"),
            HubUser(username="viewer", role="viewer", password_hash="hash")])
        for i in (1, 2):
            db.add(Site(id=i, uuid=str(i), domain=f"site{i}.example", home_url=f"https://site{i}.example", site_url=f"https://site{i}.example", status="verified"))
        db.commit()
    yield sessions, cipher
    engine.dispose()


VALUES = {"site_ids": "[1,2]", "plugin_files": '["elementor/elementor.php","elementor-pro/elementor-pro.php"]', "blocked": "true"}


def response(values):
    return {"result": {"plugins": [{"plugin_file": file, "installed": True,
        "blocked": values["blocked"], "configured": False, "verified": True} for file in values["plugin_files"]]}}


def test_ui_and_agent_queue_identical_operation_without_premature_changes(setup, monkeypatch):
    from app.api.routes import web
    sessions, cipher = setup
    calls = []
    def execute(self, site_id, ability, values, **kwargs):
        calls.append((site_id, ability, values))
        return response(values)
    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute)
    monkeypatch.setattr(web, "get_secret_cipher", lambda: cipher)
    monkeypatch.setattr(web, "require_csrf", lambda *_: None)
    with sessions() as db:
        user = db.scalar(select(HubUser).where(HubUser.username == "admin"))
        request = Request({"type": "http", "method": "POST", "path": "/updates/plugin-auto-updates", "headers": []})
        request.state.hub_user = user
        result = web.set_plugin_auto_updates(request, db, [1, 2], json.loads(VALUES["plugin_files"]), "true", "yes", "token")
        assert result.status_code == 303 and not calls
        job = db.scalar(select(HubWordPressJob))
        assert job.operation_key == "wordpress.plugins.auto_updates"
        queued = json.loads(cipher.decrypt(job.encrypted_input))
        assert queued["blocked"] == VALUES["blocked"]
        assert json.loads(queued["site_ids"]) == json.loads(VALUES["site_ids"])
        assert json.loads(queued["plugin_files"]) == json.loads(VALUES["plugin_files"])
        service = HubOperationService(db=db, cipher=cipher, actor="admin")
        service.execute("wordpress.plugins.auto_updates", VALUES)
        db.commit()
    process_next(sessions, cipher)
    process_next(sessions, cipher)
    assert calls[:2] == calls[2:]
    with sessions() as db:
        jobs = list(db.scalars(select(HubWordPressJob)))
        assert all(job.status == "succeeded" for job in jobs)
        assert len(jobs[0].result_json["plugin_auto_updates"]) == 2


@pytest.mark.parametrize("changed", [
    {"site_ids": "[]"}, {"site_ids": "[true]"}, {"site_ids": "[1,999]"},
    {"plugin_files": "[]"}, {"plugin_files": '["../bad.php"]'}, {"plugin_files": '["a/a.php",true]'},
    {"plugin_files": '["a\\\\b.php"]'}, {"blocked": "yes"}, {"blocked": ""},
    {"plugin_files": json.dumps(["a/a.php"] * 101)},
])
def test_invalid_selection_rejected_before_queue(setup, changed):
    sessions, cipher = setup
    with sessions() as db:
        with pytest.raises(HubOperationError):
            HubOperationService(db=db, cipher=cipher, actor="admin").execute("wordpress.plugins.auto_updates", {**VALUES, **changed})
        assert not db.scalars(select(HubWordPressJob)).all()


def test_permission_and_recheck_before_worker(setup, monkeypatch):
    sessions, cipher = setup
    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", lambda *a, **k: pytest.fail("No remote access"))
    with sessions() as db:
        with pytest.raises(HubOperationError):
            HubOperationService(db=db, cipher=cipher, actor="viewer").execute("wordpress.plugins.auto_updates", VALUES)
        HubOperationService(db=db, cipher=cipher, actor="admin").execute("wordpress.plugins.auto_updates", VALUES)
        db.scalar(select(HubUser).where(HubUser.username == "admin")).is_active = False
        db.commit()
    process_next(sessions, cipher)
    with sessions() as db:
        assert db.scalar(select(HubWordPressJob)).status == "failed"


@pytest.mark.parametrize("error,expected", [
    (SiteMcpProxyError("KOSMOS_BRIDGE_ABILITY_NOT_FOUND", "old", status_code=404), "unsupported"),
    (SiteMcpProxyError("REMOTE_TIMEOUT", "secret details", status_code=504), "uncertain"),
    (SiteMcpProxyError("MCP_NOT_AVAILABLE", "missing", status_code=424), "unreachable"),
    (TimeoutError("secret details"), "uncertain"),
])
def test_partial_failure_keeps_results_and_continues_without_retries(setup, monkeypatch, error, expected):
    sessions, cipher = setup
    calls = []
    def execute(self, site_id, ability, values, **kwargs):
        calls.append(site_id)
        if site_id == 1:
            raise error
        job = self.db.scalar(select(HubWordPressJob))
        assert job.result_json["plugin_auto_updates"][0]["status"] == expected
        return response(values)
    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute)
    with sessions() as db:
        HubOperationService(db=db, cipher=cipher, actor="admin").execute("wordpress.plugins.auto_updates", VALUES)
        db.commit()
    process_next(sessions, cipher)
    assert not process_next(sessions, cipher) and calls == [1, 2]
    with sessions() as db:
        job = db.scalar(select(HubWordPressJob))
        assert job.status == "failed"
        assert [row["status"] for row in job.result_json["plugin_auto_updates"]] == [expected, "succeeded"]
        assert "secret details" not in json.dumps(job.result_json)


def test_conflicting_or_missing_confirmation_never_successful():
    files = json.loads(VALUES["plugin_files"])
    payload = response({"plugin_files": files, "blocked": True})
    payload["result"]["plugins"][0]["configured"] = True
    assert Policy._verified_rows(payload, files, True)[0]["status"] == "failed"
    payload["result"]["plugins"][0]["installed"] = False
    assert Policy._verified_rows(payload, files, True)[0]["status"] == "skipped"
    payload["result"]["plugins"].pop()
    with pytest.raises(ValueError):
        Policy._verified_rows(payload, files, True)


def test_release_does_not_request_enable_and_catalog_is_generic(setup, monkeypatch):
    sessions, cipher = setup
    calls = []
    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", lambda self, site, ability, values, **kw: calls.append(values) or response(values))
    with sessions() as db:
        result = Policy(db=db, cipher=cipher).set_policy(site_ids=[1], plugin_files=["other/other.php"], blocked=False, actor="admin")
        assert result.rows[0]["plugins"][0]["status"] == "succeeded"
    assert calls == [{"plugin_files": ["other/other.php"], "blocked": False}]
    contract = get_operation("wordpress.plugins.auto_updates").input_contract()
    assert set(contract) == {"site_ids", "plugin_files", "blocked"}


def test_form_works_without_pending_updates_and_escapes_names():
    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    html = env.get_template("partials/plugin_auto_updates.html").render(csrf_token="token", plugin_options=[("one/one.php", "<script>bad</script>")])
    assert 'name="plugin_file"' in html and 'value="one/one.php"' in html
    assert "&lt;script&gt;bad&lt;/script&gt;" in html
    assert 'name="confirmed" value=""' in html


def test_route_requires_csrf_and_confirmation(setup, monkeypatch):
    from app.api.routes import web
    sessions, cipher = setup
    with sessions() as db:
        request = Request({"type": "http", "method": "POST", "path": "/updates/plugin-auto-updates", "headers": []})
        request.state.hub_user = db.scalar(select(HubUser).where(HubUser.username == "admin"))
        def reject(*args):
            raise HTTPException(403, "csrf")
        monkeypatch.setattr(web, "require_csrf", reject)
        with pytest.raises(HTTPException, match="csrf"):
            web.set_plugin_auto_updates(request, db, [1], ["a/a.php"], "true", "yes", "bad")
        monkeypatch.setattr(web, "require_csrf", lambda *_: None)
        with pytest.raises(HTTPException):
            web.set_plugin_auto_updates(request, db, [1], ["a/a.php"], "true", "", "token")
        assert not db.scalars(select(HubWordPressJob)).all()
