from datetime import UTC, datetime, timedelta
from functools import wraps
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_user import HubUser
from app.models.hub_wordpress_job import HubWordPressJob
from app.models.site import Site
from app.models.maintenance_run import MaintenanceRun
from app.models.customer import Customer
from app.models.site_snapshot import SiteSnapshot
from app.models.site_update_snapshot import SiteUpdateSnapshot
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_operations import HubOperationError, HubOperationService, get_operation
from app.services.site_users import SiteUserService
from app.services.maintenance_runs import MaintenanceRunService, PluginUpdateBatchOutcome
from app.services.wordpress_jobs import process_next, safe_outcome
from app.services.wordpress_remote_catalog import ACTIONS, execute_ui_remote


@pytest.fixture
def context(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    cipher = SecretCipher("remote-action-tests")
    with sessions() as db:
        db.add_all([HubUser(username="admin", role="admin", password_hash="hash"),
            HubUser(username="viewer", role="viewer", password_hash="hash"),
            Site(id=1, uuid="test", domain="unit.example", home_url="https://unit.example", site_url="https://unit.example", status="verified")])
        db.commit()
    yield sessions, cipher
    engine.dispose()


def gateway(db, cipher, actor="admin"):
    return HubOperationService(db=db, cipher=cipher, actor=actor)


def fake_method(monkeypatch, cls, name, callback):
    original = getattr(cls, name)
    @wraps(original)
    def replacement(self, *args, **kwargs):
        return callback(self, *args, **kwargs)
    monkeypatch.setattr(cls, name, replacement)


CREATE = {"site_id": "1", "username": "test-user", "email": "test@example.invalid", "password": "NeverStoreThisPlaintext-12345"}


def test_ui_and_agent_share_validation_defaults_and_executor(context, monkeypatch):
    sessions, cipher = context
    calls = []
    fake_method(monkeypatch, SiteUserService, "create_user", lambda _self, **values: calls.append(values) or {"id": 4, "username": values["username"]})
    with sessions() as db:
        service = gateway(db, cipher)
        execute_ui_remote(service, "wordpress.users.create", site_id=1, username=CREATE["username"], email=CREATE["email"], password=CREATE["password"])
        assert calls[0]["role"] == "subscriber" and calls[0]["display_name"] == ""
        result = service.execute("wordpress.users.create", CREATE)
        job_id = result.record_id
        assert len(calls) == 1
        job = db.get(HubWordPressJob, job_id)
        assert CREATE["password"] not in job.encrypted_input
        assert CREATE["password"] not in " ".join(get_operation("wordpress.users.create").preview(CREATE))
        db.commit()
    assert process_next(sessions, cipher)
    assert calls[0] == calls[1]
    assert not process_next(sessions, cipher)
    with sessions() as db:
        job = db.get(HubWordPressJob, job_id)
        assert job.status == "succeeded" and job.encrypted_input is None
        assert "NeverStore" not in json.dumps(gateway(db, cipher).query("wordpress.jobs.read", {"job_id": str(job_id)}))


def test_rollback_and_cancel_never_dispatch(context, monkeypatch):
    sessions, cipher = context
    calls = []
    fake_method(monkeypatch, SiteUserService, "create_user", lambda *_args, **_kwargs: calls.append(True))
    with sessions() as db:
        gateway(db, cipher).execute("wordpress.users.create", CREATE)
        db.rollback()
    assert not process_next(sessions, cipher)
    with sessions() as db:
        service = gateway(db, cipher)
        result = service.execute("wordpress.users.create", CREATE)
        service.execute("wordpress.jobs.cancel", {"job_id": str(result.record_id)})
        db.commit()
        assert db.get(HubWordPressJob, result.record_id).encrypted_input is None
    assert not process_next(sessions, cipher) and not calls


def test_rights_rechecked_at_dispatch_and_reads_are_protected(context, monkeypatch):
    sessions, cipher = context
    calls = []
    fake_method(monkeypatch, SiteUserService, "create_user", lambda *_args, **_kwargs: calls.append(True))
    with sessions() as db:
        result = gateway(db, cipher).execute("wordpress.users.create", CREATE)
        with pytest.raises(HubOperationError):
            gateway(db, cipher, "viewer").query("wordpress.jobs.read", {"job_id": str(result.record_id)})
        db.scalar(select(HubUser).where(HubUser.username == "admin")).is_active = False
        db.commit()
    assert process_next(sessions, cipher) and not calls
    with sessions() as db:
        assert db.get(HubWordPressJob, result.record_id).status == "failed"


def test_uncertain_remote_result_and_crash_are_never_retried(context, monkeypatch):
    sessions, cipher = context
    calls = []
    def fail(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError(CREATE["password"])
    fake_method(monkeypatch, SiteUserService, "create_user", fail)
    with sessions() as db:
        result = gateway(db, cipher).execute("wordpress.users.create", CREATE)
        db.commit()
    process_next(sessions, cipher)
    assert not process_next(sessions, cipher)
    assert calls == [True]
    with sessions() as db:
        job = db.get(HubWordPressJob, result.record_id)
        assert job.status == "uncertain" and job.encrypted_input is None
        assert CREATE["password"] not in job.message
        job.status, job.started_at = "running", datetime.now(UTC) - timedelta(hours=2)
        db.commit()
    assert not process_next(sessions, cipher)
    with sessions() as db:
        assert db.get(HubWordPressJob, result.record_id).status == "uncertain"


@pytest.mark.parametrize("key,values", [
    ("wordpress.users.create", {**CREATE, "role": "superuser"}),
    ("wordpress.users.password", {"site_id": "1", "user_id": "2", "password": "short"}),
    ("wordpress.updates.start", {"selected_keys": '["2|plugin|a/a.php"]'}),
    ("wordpress.updates.start", {"selected_keys": '["bad"]'}),
    ("wordpress.plugins.install", {"site_ids": '[true]', "wordpress_org_slug": "test"}),
    ("wordpress.plugins.install", {"site_ids": '[1]'}),
    ("wordpress.plugins.install", {"site_ids": '[1]', "wordpress_org_slug": "test", "package_id": "3"}),
    ("wordpress.plugins.install", {"site_ids": '[1]', "package_id": "3"}),
    ("wordpress.users.create", {**CREATE, "actor": "another-user"}),
])
def test_invalid_inputs_fail_before_job_creation(context, key, values):
    sessions, cipher = context
    with sessions() as db:
        with pytest.raises(ValueError):
            gateway(db, cipher).execute(key, values)
        assert not db.scalars(select(HubWordPressJob)).all()


def test_queued_update_is_submitted_not_successful(context, monkeypatch):
    sessions, cipher = context
    def start(self, **kwargs):
        run = MaintenanceRun(site_id=1, kind="direct-plugin-update", status="running", requested_by=kwargs["actor"], started_at=datetime.now(UTC), result_json={"batch_id": "a" * 32})
        self.db.add(run)
        self.db.commit()
        return PluginUpdateBatchOutcome(batch_id="a" * 32, run_count=1, message="queued")
    fake_method(monkeypatch, MaintenanceRunService, "start_direct_updates", start)
    from app.services import maintenance_worker
    monkeypatch.setattr(maintenance_worker, "schedule_pending_direct_updates", lambda: None)
    with sessions() as db:
        result = gateway(db, cipher).execute("wordpress.updates.start", {"selected_keys": '["1|plugin|a/a.php"]'})
        db.commit()
    process_next(sessions, cipher)
    with sessions() as db:
        data = gateway(db, cipher).query("wordpress.jobs.read", {"job_id": str(result.record_id)})
        assert data["status"] == "submitted"
        assert data["runs"][0]["status"] == "running"


def test_all_catalog_actions_expose_domain_signatures_and_can_queue(context):
    for key, spec in ACTIONS.items():
        operation = get_operation(key)
        assert operation is not None
        assert set(operation.input_contract()) == set(spec.parameters())
        assert "actor" not in operation.input_contract()


def test_job_page_renders_without_credentials(context):
    from jinja2 import Environment, ChoiceLoader, DictLoader, FileSystemLoader
    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/wordpress/jobs/1", "query_string": b"", "headers": []})
    env = Environment(loader=ChoiceLoader([DictLoader({"base.html": "{% block content %}{% endblock %}"}), FileSystemLoader("app/templates")]), autoescape=True)
    html = env.get_template("wordpress_job.html").render(request=request,
        job={"job_id": "1", "operation": "wordpress.users.create", "status": "queued", "message": "Waiting", "runs": [], "can_cancel": True}, csrf_token="token")
    assert "Auftrag abbrechen" in html and "Status aktualisieren" in html
    assert CREATE["password"] not in html


def test_restricted_customer_scope_blocks_reads_ui_and_agent(context):
    sessions, cipher = context
    with sessions() as db:
        access = HubAccessControlService(db=db)
        access.save_role(role_key="web-editor", name="Web", description="test", permissions={
            "websites": {"view": True, "edit": True, "scope": "all"},
            "customers": {"view": True, "scope": "none"},
        })
        db.add(HubUser(username="editor", role="web-editor", password_hash="hash"))
        customer = Customer(name="Hidden customer")
        db.add(customer)
        db.flush()
        db.get(Site, 1).customer_id = customer.id
        db.flush()
        service = gateway(db, cipher, "editor")
        for key in ("wordpress.backups.list", "wordpress.updates.read", "wordpress.updates.options", "wordpress.runs.list", "wordpress.users.list"):
            with pytest.raises(HubOperationError):
                service.query(key, {"site_id": "1"})
        with pytest.raises(HubOperationError):
            service.execute("wordpress.backups.create", {"site_id": "1"})
        with pytest.raises(HubOperationError):
            execute_ui_remote(service, "wordpress.backups.create", site_id=1)
        assert not db.scalars(select(HubWordPressJob)).all()


def test_readers_are_local_and_exclude_package_secrets(context, monkeypatch):
    from app.api.routes.site_updates import get_latest_site_updates
    from app.services.site_mcp_proxy import SiteMcpProxyService
    from starlette.requests import Request
    sessions, cipher = context
    def unexpected(*args, **kwargs):
        raise AssertionError("Local readers must not contact WordPress")
    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", unexpected)
    with sessions() as db:
        db.add(SiteSnapshot(site_id=1, captured_at=datetime.now(UTC), plugins_json=[
            {"name": "Plugin", "plugin_file": "a/a.php", "version": "1", "is_active": True}], themes_json=[], environment_json={}))
        db.add(SiteUpdateSnapshot(site_id=1, captured_at=datetime.now(UTC), core_updates_json=[], theme_updates_json=[],
            plugin_updates_json=[{"name": "Plugin", "plugin_file": "a/a.php", "current_version": "1", "new_version": "2", "package": "SECRET"}], summary_json={}))
        db.commit()
        service = gateway(db, cipher)
        request = Request({"type": "http", "path": "/api/v1/sites/1/updates/latest", "headers": []})
        request.state.hub_user = db.scalar(select(HubUser).where(HubUser.username == "admin"))
        api = get_latest_site_updates(1, request, db, cipher)
        data = service.query("wordpress.updates.read", {"site_id": "1"})
        from app.core.timezones import iso_berlin_time
        assert data["captured_at"] == iso_berlin_time(api.captured_at)
        assert data["items"][0]["new_version"] == "2"
        assert "SECRET" not in json.dumps(data)
        for key in ("wordpress.backups.list", "wordpress.updates.options", "wordpress.users.list", "wordpress.runs.list"):
            service.query(key, {"site_id": "1"})
        assert not db.new and not db.dirty and not db.deleted


def test_password_erased_before_domain_call_and_cancel_after_dispatch_rejected(context, monkeypatch):
    sessions, cipher = context
    def inspect_input(self, **kwargs):
        job = self.db.scalar(select(HubWordPressJob))
        assert job.encrypted_input is None and job.status == "running"
        with pytest.raises(HubOperationError):
            gateway(self.db, cipher).execute("wordpress.jobs.cancel", {"job_id": str(job.id)})
        assert kwargs["password"] == CREATE["password"]
        return {"id": 42}
    fake_method(monkeypatch, SiteUserService, "create_user", inspect_input)
    with sessions() as db:
        job_id = gateway(db, cipher).execute("wordpress.users.create", CREATE).record_id
        db.commit()
    process_next(sessions, cipher)
    with sessions() as db:
        assert db.get(HubWordPressJob, job_id).status == "succeeded"


def test_batch_without_loaded_runs_is_never_reported_complete(context, monkeypatch):
    sessions, cipher = context
    fake_method(monkeypatch, MaintenanceRunService, "start_direct_updates", lambda *_a, **_kw:
        PluginUpdateBatchOutcome(batch_id="b" * 32, run_count=1, message="queued"))
    from app.services import maintenance_worker
    monkeypatch.setattr(maintenance_worker, "schedule_pending_direct_updates", lambda: None)
    with sessions() as db:
        result = gateway(db, cipher).execute("wordpress.updates.start", {"selected_keys": '["1|plugin|a/a.php"]'})
        db.commit()
    process_next(sessions, cipher)
    with sessions() as db:
        assert db.get(HubWordPressJob, result.record_id).status == "submitted"


def test_user_reader_exposes_exact_selection_keys(context, monkeypatch):
    sessions, cipher = context
    fake_method(monkeypatch, SiteUserService, "get_latest_inventory", lambda *_a, **_kw:
        SimpleNamespace(snapshot=SimpleNamespace(available=True, captured_at=datetime.now(UTC)), users=[
            {"id": 72, "username": "editor", "display_name": "Editor", "email": "editor@example.invalid", "roles": ["editor"], "private": "SECRET"}]))
    with sessions() as db:
        result = gateway(db, cipher).query("wordpress.users.list", {"site_id": "1"})
        assert result["available"] and result["items"][0]["selected_key"] == "1:72"
        assert "SECRET" not in json.dumps(result)
