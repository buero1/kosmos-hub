from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.maintenance_run import MaintenanceRun
from app.models.site import Site
from app.services.maintenance_runs import MaintenanceRunService
from app.services.site_mcp_proxy import SiteMcpProxyError


@pytest.fixture
def service():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield MaintenanceRunService(db=db, cipher=None)
    engine.dispose()


def add_site(service, name="bridge-test"):
    site = Site(uuid=name, domain=name + ".example", home_url="https://example.test/",
                site_url="https://example.test/", status="verified", bridge_version="0.3.68")
    service.db.add(site)
    service.db.commit()
    return site


def entry(name):
    return SimpleNamespace(kind="plugin", name=name, identifier=name + "/" + name + ".php",
                           current_version="0.3.68", target_version="0.3.70", is_active=True, update_available=True)


def test_complete_update_stops_at_lost_bridge_and_records_unexecuted_updates(service, monkeypatch):
    site = add_site(service)
    parent = service.start_complete_site_update(site_id=site.id, actor="test").run
    plugins = [entry("first"), entry("kosmos-bridge"), entry("mainwp"), entry("updraftplus")]
    executed = []
    phases = []

    def fresh(run, *, phase, wave):
        phases.append(phase)
        if phase == "plugin":
            run.result_json = {**run.result_json, "pending_updates": [service._complete_site_update_entry_summary(p) for p in plugins]}
            return plugins, None
        return [], None

    def poll(child):
        executed.append(child.result_json["update_name"])
        if len(executed) == 1:
            child.status = "succeeded"
            child.result_json = {**child.result_json, "stage_message": "OK"}
            return "succeeded"
        service._record_bridge_connection_failure(child, SiteMcpProxyError("REST_NO_ROUTE", "No route", status_code=404))
        service._fail_plugin_update_run(child, "Self-update failed, Bridge unreachable")
        return "failed"

    monkeypatch.setattr(service, "_fresh_complete_site_update_entries", fresh)
    monkeypatch.setattr(service, "_complete_site_update_entries_by_readiness", lambda entries: (entries, []))
    monkeypatch.setattr(service, "_poll_plugin_update", poll)
    assert service.poll_complete_site_update_run(parent.id) == "failed"
    assert executed == ["first", "kosmos-bridge"]
    assert phases == ["wordpress", "theme", "plugin"]
    assert parent.result_json["stage"] == "bridge-unavailable"
    assert parent.result_json["successful_updates"] == 1
    assert parent.result_json["failed_updates"] == 1
    assert parent.result_json["skipped_updates"] == 2
    skipped = [event for event in parent.result_json["events"] if event["status"] == "skipped"]
    assert [event["update"]["name"] for event in skipped] == ["mainwp", "updraftplus"]
    assert all(event["detail"] == "Nicht ausgeführt: Bridge nicht erreichbar." for event in skipped)
    assert len(service.complete_site_update_child_runs(parent.id)) == 2


def test_reconciliation_records_bridge_loss_but_does_not_retry_update(service, monkeypatch):
    site = add_site(service)
    run = SimpleNamespace(site_id=site.id, result_json={})
    details = {"update_kind": "plugin", "update_identifier": "kosmos-bridge/kosmos-bridge.php",
               "target_version": "0.3.70", "expected_active": True, "update_name": "Kosmos Bridge"}
    monkeypatch.setattr(service, "_start_plugin_update_step", lambda *args: None)
    reads = []

    def read(*args, **kwargs):
        reads.append(args)
        raise SiteMcpProxyError("REST_NO_ROUTE", "No route", status_code=404)

    monkeypatch.setattr(service.proxy, "execute_readonly_ability", read)
    monkeypatch.setattr(service.proxy, "execute_ability", lambda *a, **k: pytest.fail("Must not repeat the update"))
    result, _ = service._reconcile_plugin_after_failed_update_request(
        run, details, None, SiteMcpProxyError("ABILITY_CALLBACK_EXCEPTION", "Undefined method", status_code=500),
    )
    assert result is None
    assert len(reads) == 1
    assert run.result_json["bridge_unavailable"]["code"] == "REST_NO_ROUTE"
    assert run.result_json["post_update_reconciliation"]["original_error_code"] == "ABILITY_CALLBACK_EXCEPTION"


def test_direct_batch_skips_only_queued_updates_of_the_affected_site(service):
    site = add_site(service)
    other = add_site(service, "healthy")
    batch = "a" * 32
    runs = []
    for target, stage in [(site, "processing"), (site, "queued"), (other, "queued"), (site, "processing")]:
        run = MaintenanceRun(site=target, kind=service.PLUGIN_UPDATE_KIND, status="running", requested_by="test",
                             started_at=datetime.now(UTC), result_json={"batch_id": batch, "stage": stage})
        service.db.add(run)
        runs.append(run)
    service.db.commit()
    service._record_bridge_connection_failure(runs[0], SiteMcpProxyError("REMOTE_TIMEOUT", "Offline", status_code=504))
    service._fail_plugin_update_run(runs[0], "Bridge offline")
    assert [run.status for run in runs] == ["failed", "skipped", "running", "running"]
    assert runs[1].result_json["stage_message"] == "Nicht ausgeführt: Bridge nicht erreichbar."


@pytest.mark.parametrize("code,status,blocked", [
    ("REST_NO_ROUTE", 404, True), ("REMOTE_INVALID_RESPONSE", 200, True),
    ("REMOTE_TIMEOUT", 504, True), ("KOSMOS_BRIDGE_AUTH_FAILED", 401, True),
    ("ABILITY_CALLBACK_EXCEPTION", 500, False), ("KOSMOS_BRIDGE_UPDATE_FAILED", 500, False),
    ("KOSMOS_BRIDGE_UPDATE_OFFER_CHANGED", 409, False),
])
def test_connection_failure_is_distinct_from_a_plugin_failure(code, status, blocked):
    run = SimpleNamespace(result_json={})
    MaintenanceRunService._record_bridge_connection_failure(run, SiteMcpProxyError(code, "test", status_code=status))
    assert bool(run.result_json.get("bridge_unavailable")) == blocked


@pytest.mark.parametrize("response", ["ok", "offline", "inactive", "wrong-version", "invalid"])
def test_bridge_success_requires_a_fresh_request_even_in_complete_workflow(service, monkeypatch, response):
    reachable = response == "ok"
    site = add_site(service)
    parent = service.start_complete_site_update(site_id=site.id, actor="test").run
    child = service._create_complete_site_update_child_run(parent, entry("kosmos-bridge"), phase="plugin", wave=1)
    monkeypatch.setattr(service, "_activate_crocoblock_license_if_required", lambda *a, **k: None)
    monkeypatch.setattr(service, "_execute_direct_update", lambda *a: {"result": {
        "updated": True, "plugin_file": "kosmos-bridge/kosmos-bridge.php", "previous_version": "0.3.68",
        "installed_version": "0.3.70", "active": True,
    }})
    reads = []

    def read(*args, **kwargs):
        reads.append(args)
        if response == "offline":
            raise SiteMcpProxyError("REST_NO_ROUTE", "No route", status_code=404)
        if response == "invalid":
            return {"result": {"plugins": None}}
        return {"result": {"plugins": [{"plugin_file": "kosmos-bridge/kosmos-bridge.php",
                                          "version": "0.3.69" if response == "wrong-version" else "0.3.70",
                                          "active": response != "inactive"}]}}

    monkeypatch.setattr(service.proxy, "execute_readonly_ability", read)
    monkeypatch.setattr(service, "_complete_confirmed_direct_update_without_postflight_health", lambda *a: "succeeded")
    assert service._poll_plugin_update(child) == ("succeeded" if reachable else "failed")
    assert len(reads) == 1
    assert bool(child.result_json.get("bridge_fresh_request_verified")) == reachable
    assert bool(child.result_json.get("bridge_unavailable")) != reachable


def test_plugin_error_with_reachable_bridge_does_not_block_other_updates(service, monkeypatch):
    run = SimpleNamespace(site_id=1, result_json={"bridge_unavailable": {"code": "REMOTE_TIMEOUT"}})
    details = {"update_kind": "plugin", "update_identifier": "test/test.php", "target_version": "2", "expected_active": True}
    monkeypatch.setattr(service, "_start_plugin_update_step", lambda *a: None)
    monkeypatch.setattr(service.proxy, "execute_readonly_ability", lambda *a, **k: {"result": {
        "plugins": [{"plugin_file": "test/test.php", "version": "1", "active": True}],
    }})
    result, _ = service._reconcile_plugin_after_failed_update_request(
        run, details, None, SiteMcpProxyError("REMOTE_TIMEOUT", "Timed out", status_code=504),
    )
    assert result is None
    assert "bridge_unavailable" not in run.result_json


def test_empty_json_is_not_confirmation_of_a_healthy_bridge(service, monkeypatch):
    run = SimpleNamespace(site_id=1, result_json={})
    details = {"update_kind": "plugin", "update_identifier": "test/test.php", "target_version": "2", "expected_active": True}
    monkeypatch.setattr(service, "_start_plugin_update_step", lambda *a: None)
    monkeypatch.setattr(service.proxy, "execute_readonly_ability", lambda *a, **k: {"result": {}})
    result, _ = service._reconcile_plugin_after_failed_update_request(
        run, details, None, SiteMcpProxyError("ABILITY_CALLBACK_EXCEPTION", "Callback failed"),
    )
    assert result is None
    assert run.result_json["bridge_unavailable"]["code"] == "REMOTE_INVALID_RESPONSE"


def test_bridge_loss_during_refresh_skips_known_later_phases(service, monkeypatch):
    site = add_site(service)
    parent = service.start_complete_site_update(site_id=site.id, actor="test").run
    pending = [entry("theme"), entry("plugin")]
    pending[0].kind = "theme"
    parent.result_json = {**parent.result_json, "pending_updates": [service._complete_site_update_entry_summary(p) for p in pending]}
    service.db.commit()

    def fresh(run, **kwargs):
        service._record_bridge_connection_failure(run, SiteMcpProxyError("REST_NO_ROUTE", "No route", status_code=404))
        return [], "No route"

    monkeypatch.setattr(service, "_fresh_complete_site_update_entries", fresh)
    monkeypatch.setattr(service, "_poll_plugin_update", lambda *a: pytest.fail("Must not start updates"))
    assert service.poll_complete_site_update_run(parent.id) == "failed"
    assert parent.result_json["skipped_updates"] == 2
    assert parent.result_json["failed_updates"] == 0
    assert parent.result_json["stage"] == "bridge-unavailable"
