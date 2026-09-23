import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, event
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from fastapi import BackgroundTasks

from app.db.base import Base
from app.core.security import SecretCipher
from app.models.hub_user import HubUser
from app.models.site import Site
from app.models.customer import Customer
from app.models.fleet_refresh_run import FleetRefreshRun
from app.models.site_snapshot import SiteSnapshot
from app.models.maintenance_run import MaintenanceRun
from app.services.hub_operations import HubOperationService, HubOperationError
from app.services.hub_access_control import HubAccessControlService
from app.services.fleet_refresh import FleetRefreshService
from app.services.wordpress_workbench import dashboard_data, batch_status


def test_fleet_status_does_not_load_website_histories(env):
    from app.services.wordpress_workbench import fleet_status
    db, _sessions, service = env
    for identifier in range(2, 134):
        db.add(Site(id=identifier, uuid=str(identifier), domain=f'{identifier}.example',
            home_url=f'https://{identifier}.example', site_url=f'https://{identifier}.example', status='verified'))
    run = FleetRefreshRun(mode='fresh-updates', status='running', requested_by='admin',
        result_json=FleetRefreshService._initial_result('fresh-updates', target_site_ids=set(range(1, 134))))
    db.add(run)
    db.commit()
    run_id = run.id
    queries = []
    def capture(_conn, _cursor, statement, *_args):
        queries.append(statement.lower())
    event.listen(db.bind, 'before_cursor_execute', capture)
    try:
        assert fleet_status(service, run_id)['status'] == 'running'
    finally:
        event.remove(db.bind, 'before_cursor_execute', capture)
    assert len(queries) <= 8
    assert not any(name in query for query in queries for name in (
        'site_snapshots', 'site_update_snapshots', 'site_connections', 'site_capabilities', 'audit_logs'))
    db.delete(db.get(Site, 133))
    db.flush()
    with pytest.raises(HubOperationError):
        fleet_status(service, run_id)


def test_bulk_website_check_still_enforces_customer_rights(env):
    from app.services.hub_operation_websites import require_website_selection_access
    db, _sessions, service = env
    access = HubAccessControlService(db=db)
    access.save_role(role_key='limited', name='Limited', description='', permissions={
        'websites': {'view': True, 'scope': 'all'}, 'customers': {'view': True, 'scope': 'none'}})
    db.add(HubUser(username='limited', role='limited', password_hash='hash'))
    customer = Customer(name='Hidden')
    db.add(customer)
    db.flush()
    service.actor = 'limited'
    require_website_selection_access(service, {1})
    db.get(Site, 1).customer_id = customer.id
    db.flush()
    with pytest.raises(HubOperationError):
        require_website_selection_access(service, {1})
    with pytest.raises(HubOperationError):
        require_website_selection_access(service, {999})
    user = db.scalar(select(HubUser).where(HubUser.username == 'limited'))
    user.is_active = False
    db.flush()
    with pytest.raises(HubOperationError):
        require_website_selection_access(service, {1})


@pytest.fixture
def env():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    cipher = SecretCipher("test-wordpress-workbench")
    with sessions() as db:
        db.add_all([HubUser(username="admin", role="admin", password_hash="hash"),
            HubUser(username="viewer", role="viewer", password_hash="hash"),
            Site(id=1, uuid="one", domain="one.example", home_url="https://one.example", site_url="https://one.example", status="verified")])
        db.commit()
        yield db, sessions, HubOperationService(db=db, cipher=cipher, actor="admin")
    engine.dispose()


def request_for(db):
    request = Request({"type": "http", "method": "POST", "path": "/updates/fresh-show", "headers": [], "query_string": b"", "session": {}})
    request.state.hub_user = db.scalar(select(HubUser).where(HubUser.username == "admin"))
    return request


def test_shared_fleet_requires_explicit_targets_and_rolls_back(env):
    db, _sessions, service = env
    for ids in ("", "[]", "[true]", "[0]", "[999]", '"all"'):
        with pytest.raises(HubOperationError):
            service.execute("wordpress.fleet.start", {"mode": "fresh-updates", "site_ids": ids})
    result = service.execute("wordpress.fleet.start", {"mode": "fresh-updates", "site_ids": "[1]"})
    run = db.get(FleetRefreshRun, result.record_id)
    assert run.status == "queued" and not run.allow_provider_activation
    assert run.result_json["scope"]["site_ids"] == [1]
    assert run.result_json["shared_contract"]
    assert result.outputs["created"] == "true"
    db.rollback()
    assert not db.scalars(select(FleetRefreshRun)).all()


def test_browser_and_agent_share_fleet_start_and_cancellation(env, monkeypatch):
    from app.api.routes import web
    db, _sessions, service = env
    monkeypatch.setattr(web, "require_csrf", lambda *_a: None)
    tasks = BackgroundTasks()
    response = web.show_fresh_updates(request_for(db), tasks, db, q="", site_id=[1], site_scope="selected", plugin="", kind="all", activity="all", diagnosis="all", csrf_token="test")
    run = db.scalar(select(FleetRefreshRun))
    assert response.status_code == 303 and run.result_json["shared_contract"]
    assert len(tasks.tasks) == 1  # Task is registered, not executed in this test.
    second = service.execute("wordpress.fleet.start", {"mode": "fresh-users", "site_ids": "[1]"})
    assert second.outputs["created"] == "false" and second.record_id == run.id
    result = service.execute("wordpress.fleet.cancel", {"run_id": str(run.id)})
    assert result.outputs["cancelled"] == "true"
    assert service.query("wordpress.fleet.status", {"run_id": str(run.id)})["status"] == "cancelled"


def test_fleet_worker_rechecks_revoked_actor_before_network(env, monkeypatch):
    from app.services import fleet_refresh
    db, sessions, service = env
    result = service.execute("wordpress.fleet.start", {"mode": "fresh-users", "site_ids": "[1]"})
    db.commit()
    actor = db.scalar(select(HubUser).where(HubUser.username == "admin"))
    actor.is_active = False
    db.commit()
    monkeypatch.setattr(fleet_refresh, "SessionLocal", sessions)
    assert FleetRefreshService._claim_run(result.record_id) is None
    db.expire_all()
    assert db.get(FleetRefreshRun, result.record_id).status == "failed"


def test_workbench_counts_and_apis_hide_customer_scoped_sites(env):
    from app.api.routes.site_inventory import get_latest_site_snapshot
    from fastapi import HTTPException
    db, _sessions, service = env
    access = HubAccessControlService(db=db)
    access.save_role(role_key="limited", name="Limited", description="test", permissions={
        "dashboard": {"view": True}, "websites": {"view": True, "edit": True, "scope": "all"}, "customers": {"view": True, "scope": "none"}})
    actor = HubUser(username="limited", role="limited", password_hash="hash")
    customer = Customer(name="Hidden")
    db.add_all([actor, customer])
    db.flush()
    db.get(Site, 1).customer_id = customer.id
    db.flush()
    service.actor = actor.username
    assert dashboard_data(service)["summary"]["total_sites"] == 0
    assert service.query("wordpress.workbench.updates", {})["total"] == 0
    for key in ("wordpress.capabilities.list", "wordpress.state.read"):
        with pytest.raises(HubOperationError):
            service.query(key, {"site_id": "1"})
    request = request_for(db)
    request.state.hub_user = actor
    with pytest.raises(HTTPException) as exc:
        get_latest_site_snapshot(1, request, db, service.cipher)
    assert exc.value.status_code == 403


def test_state_query_never_exposes_environment_or_plugin_secrets(env):
    db, _sessions, service = env
    db.add(SiteSnapshot(site_id=1, captured_at=datetime.now(UTC), wordpress_version="6.8", php_version="8.3",
        plugins_json=[{"name": "Plugin", "plugin_file": "a/a.php", "version": "1", "license_key": "SECRET"}], themes_json=[], environment_json={"password": "SECRET"}))
    db.commit()
    data = service.query("wordpress.state.read", {"site_id": "1"})
    assert data["wordpress_version"] == "6.8"
    assert "SECRET" not in json.dumps(data)
    assert not db.new and not db.dirty and not db.deleted


def test_batch_status_shared_payload_and_explicit_pagination(env):
    db, _sessions, service = env
    for index in range(26):
        db.add(MaintenanceRun(site_id=1, kind="direct-plugin-update", status="running", requested_by="admin",
            started_at=datetime.now(UTC), result_json={"batch_id": "a" * 32, "batch_position": index, "stage": "queued"}))
    db.commit()
    ui = batch_status(service, "a" * 32)
    agent = service.query("wordpress.updates.batch_status", {"batch_id": "a" * 32})
    assert agent["total"] == ui["total"] == 26
    assert agent["runs"] == ui["runs"][:25]
    assert agent["pagination"]["runs"]["next_offset"] == "25"
    more = service.query("wordpress.updates.batch_status", {"batch_id": "a" * 32, "offset": "25"})
    assert len(more["runs"]) == 1


def test_registration_removal_preserves_real_or_linked_sites(env):
    db, _sessions, service = env
    with pytest.raises(HubOperationError):
        service.execute("websites.remove_test_registration", {"site_id": "1"})
    site = db.get(Site, 1)
    site.domain = "test-one.kosmos-medien.de"
    db.commit()
    result = service.execute("websites.remove_test_registration", {"site_id": "1"})
    assert result.href.startswith("/sites?removed=")
    db.rollback()
    assert db.get(Site, 1) is not None
    customer = Customer(name="Keep me")
    db.add(customer)
    db.flush()
    site.customer_id = customer.id
    with pytest.raises(HubOperationError):
        service.execute("websites.remove_test_registration", {"site_id": "1"})


def test_admin_only_fleet_and_user_reads(env):
    _db, _sessions, service = env
    service.actor = "viewer"
    for key in ("wordpress.workbench.users", "wordpress.workbench.backups"):
        with pytest.raises(HubOperationError):
            service.query(key, {})
    with pytest.raises(HubOperationError):
        service.execute("wordpress.fleet.start", {"mode": "fresh-updates", "site_ids": "[1]"})


def test_dashboard_without_website_permission_has_empty_summary(env):
    db, _sessions, service = env
    role = HubAccessControlService(db=db).save_role(role_key="dashboard_only", name="Dashboard", description="test",
        permissions={"dashboard": {"view": True}})
    db.add(HubUser(username="dashboard_only", role=role.key, password_hash="hash"))
    db.flush()
    service.actor = "dashboard_only"
    data = dashboard_data(service)
    assert data["summary"]["total_sites"] == 0 and data["sites"] == []


def test_legacy_assistant_uses_shared_record_permissions(env):
    from app.services.assistant_tools import HubAssistantTools, AssistantToolError
    db, _sessions, service = env
    HubAccessControlService(db=db).save_role(role_key="scoped", name="Scoped", description="test", permissions={
        "websites": {"view": True, "scope": "all"}, "customers": {"view": True, "scope": "none"}})
    db.add(HubUser(username="scoped", role="scoped", password_hash="hash"))
    customer = Customer(name="Hidden customer")
    db.add(customer)
    db.flush()
    db.get(Site, 1).customer_id = customer.id
    db.flush()
    assistant = HubAssistantTools(db=db, cipher=service.cipher, panel_site_ids=None, actor="scoped")
    assert assistant.items == []
    assert assistant._search_customers({"query": "Hidden"})["matches"] == []
    with pytest.raises(AssistantToolError):
        assistant._list_wordpress_users({"scope": "all", "limit": 10})


@pytest.mark.parametrize("endpoint", ["discover_site_abilities", "get_site_ability_info", "execute_site_ability"])
def test_diagnostics_require_manage_permission_before_remote_call(env, monkeypatch, endpoint):
    from app.api.routes import site_abilities
    from app.schemas.ability import ExecuteAbilityRequest
    from fastapi import HTTPException
    db, _sessions, service = env
    request = request_for(db)
    request.state.hub_user = db.scalar(select(HubUser).where(HubUser.username == "viewer"))
    def forbidden_proxy(**_kwargs):
        pytest.fail("No remote client may be created before the permission check")
    monkeypatch.setattr(site_abilities, "SiteMcpProxyService", forbidden_proxy)
    extra = {"ability_name": "read-test"} if endpoint == "get_site_ability_info" else {}
    if endpoint == "execute_site_ability":
        extra["body"] = ExecuteAbilityRequest(ability_name="read-test", input={})
    with pytest.raises(HTTPException) as exc:
        getattr(site_abilities, endpoint)(site_id=1, request=request, db=db, cipher=service.cipher, **extra)
    assert exc.value.status_code == 403
