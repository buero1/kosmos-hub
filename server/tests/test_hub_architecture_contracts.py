from copy import deepcopy
import json

from scripts.check_hub_contracts import MANIFEST, inspect_file, inventory, validate, validate_catalog, coverage_report


def test_all_http_capabilities_have_reviewed_contracts():
    routes, files = inventory()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert not (errors := validate(routes, files, manifest)), "\n".join(errors)
    assert not (errors := validate_catalog(routes, manifest)), "\n".join(errors)


def example():
    source = '''
@router.post("/example")
def action():
    return helper()

def helper():
    return HubOperationService().execute("example.create", {})
'''
    routes, fingerprint = inspect_file(source, "routes.py")
    identity = next(iter(routes))
    files = {"routes.py": fingerprint}
    manifest = {"files": files, "routes": {identity: {"state": "shared", "reason": "Shared example",
        "contracts": ["example.create"], "boundary": ["HubOperationService", "execute"], "fingerprint": routes[identity]["fingerprint"]}}}
    return source, routes, files, manifest


def test_new_route_and_new_api_file_are_not_grandfathered():
    source, routes, files, manifest = example()
    extra, _ = inspect_file('@router.post("/new")\ndef another():\n    db.add(record)\n', "new.py")
    assert any("NEW route" in error for error in validate({**routes, **extra}, files, manifest))
    assert any("wiring" in error for error in validate(routes, {**files, "new.py": "new"}, manifest))


def test_route_and_transitive_helper_changes_require_review():
    source, _, files, manifest = example()
    for changed in (source.replace("return helper()", "db.add(record)\n    return helper()"),
                    source.replace('return HubOperationService()', 'db.delete(record)\n    return HubOperationService()')):
        routes, _ = inspect_file(changed, "routes.py")
        assert any("Changed route/helper" in error for error in validate(routes, files, manifest))


def test_removed_boundary_fails_even_if_hash_was_refreshed():
    source, _, files, manifest = example()
    routes, _ = inspect_file(source.replace('HubOperationService().execute("example.create", {})', 'db.add(record)'), "routes.py")
    identity = next(iter(routes))
    manifest["routes"][identity]["fingerprint"] = routes[identity]["fingerprint"]
    assert any("boundary bypassed" in error for error in validate(routes, files, manifest))


def test_formatting_does_not_require_review_but_changed_pending_routes_do():
    source, routes, files, manifest = example()
    formatted, _ = inspect_file(source.replace("return helper()", "# Formatting is not a capability\n    return   helper()"), "routes.py")
    assert not validate(formatted, files, manifest)
    pending = deepcopy(manifest)
    next(iter(pending["routes"].values()))["state"] = "pending"
    changed, _ = inspect_file(source.replace("return helper()", "return new_logic()"), "routes.py")
    assert any("Changed route/helper" in error for error in validate(changed, files, pending))


def test_unknown_catalog_key_is_rejected():
    _, routes, _, manifest = example()
    assert any("Unknown catalog key" in error for error in validate_catalog(routes, manifest))


def test_keyword_bound_reader_is_checked_too():
    from functools import partial
    from scripts.check_hub_contracts import callback_calls
    from app.services.hub_operation_wordpress_workbench import status_query
    from app.services.wordpress_workbench import batch_status
    calls = callback_calls(partial(status_query, reader=batch_status, id_field="batch_id"))
    assert "maintenance_batch" in calls


def test_pending_needs_a_real_backlog_group():
    _, routes, files, manifest = example()
    entry = next(iter(manifest["routes"].values()))
    entry["state"] = "pending"
    assert any("migration group" in error for error in validate(routes, files, manifest))
    entry["migration_group"] = "layouts"
    assert not validate(routes, files, manifest)


def test_scope_exclusions_need_a_decision_and_still_guard_route_changes():
    source, routes, files, manifest = example()
    entry = next(iter(manifest["routes"].values()))
    entry.update(state="out_of_scope", scope_exclusion="zoho-retirement")
    assert any("scope decision" in error for error in validate(routes, files, manifest))
    manifest["scope_exclusions"] = {"zoho-retirement": {"reason": "Explicit user scope decision"}}
    assert any("shared coverage" in error for error in validate(routes, files, manifest))
    entry.pop("contracts")
    entry.pop("boundary")
    assert not validate(routes, files, manifest)
    changed, _ = inspect_file(source.replace("return helper()", "return replaced()"), "routes.py")
    assert any("Changed route/helper" in error for error in validate(changed, files, manifest))


def test_zoho_is_excluded_without_disguising_local_crm_as_retired():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for identity, entry in manifest["routes"].items():
        if "zoho" in identity.split(":")[1]:
            assert entry["state"] == "out_of_scope"
            assert entry["scope_exclusion"] == "zoho-retirement"
        if identity.split(":")[1] in {"create_customer_page", "create_lead_page", "update_customer_fields", "update_lead_fields"}:
            assert entry["state"] == "shared"
    report = coverage_report(manifest)
    assert "out_of_scope: 41" in report
    retry = next(entry for identity, entry in manifest["routes"].items() if ":retry_failed_mailbox_message:" in identity)
    assert retry["scope_exclusion"] == "imports-and-sync"
    assert "Scope exclusions are not completed migrations" in report


def test_ci_runs_the_whole_parity_suite_not_only_the_latest_module():
    workflow = MANIFEST.parents[3] / ".github/workflows/hub-contracts.yml"
    assert "run: python -m pytest -q" in workflow.read_text(encoding="utf-8")
