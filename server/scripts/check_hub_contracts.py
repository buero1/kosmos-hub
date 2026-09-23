"""Fail closed on new or changed HTTP capabilities until their contract is reviewed.

The manifest is a reviewed migration ledger, not an automatically refreshed allowlist.
AST hashes ignore formatting/comments and include route-local helper functions.
"""

import ast
from collections import Counter
from hashlib import sha256
import inspect
import json
from pathlib import Path
import sys
from functools import partial
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "architecture" / "hub_http_contracts.json"
METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "websocket", "api_route"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
STATES = {"shared", "pending", "human_only", "infrastructure", "out_of_scope"}
PENDING_GROUPS = {
    "websites", "settings", "access_control", "layouts", "pdf_templates",
    "legal_terms", "mailbox", "crm_reads", "calendar", "desktop_reminders",
}


def digest(nodes):
    return sha256("\n".join(ast.dump(node, include_attributes=False) for node in nodes).encode()).hexdigest()


def closure(function, functions):
    seen = {}
    def visit(node):
        if node.name in seen:
            return
        seen[node.name] = node
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in functions:
                visit(functions[call.func.id])
    visit(function)
    return [seen[name] for name in sorted(seen)]


def inspect_file(source, filename):
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, FUNCTIONS)}
    routes = {}
    for function in functions.values():
        for decorator in function.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute) or decorator.func.attr not in METHODS:
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant) or not isinstance(decorator.args[0].value, str):
                raise ValueError(f"Dynamic route requires explicit inventory support: {filename}:{function.name}")
            identity = f"{filename}:{function.name}:{decorator.func.attr.upper()}:{decorator.args[0].value}"
            nodes = closure(function, functions)
            routes[identity] = {"fingerprint": digest(nodes), "nodes": nodes}
    # Also lock router construction, imports and out-of-function route registration.
    scaffold = [node for node in tree.body if not isinstance(node, FUNCTIONS)]
    return routes, digest(scaffold)


def inventory(root=ROOT):
    routes, files = {}, {}
    for path in sorted([*(root / "app" / "api").rglob("*.py"), root / "app" / "main.py"]):
        name = path.relative_to(root).as_posix()
        found, fingerprint = inspect_file(path.read_text(encoding="utf-8-sig"), name)
        routes.update(found)
        files[name] = fingerprint
    # A new router cannot evade discovery by being mounted from main.py.
    main = ast.parse((root / "app" / "main.py").read_text(encoding="utf-8-sig"))
    files["app/main.py:route-wiring"] = digest([node for node in ast.walk(main) if isinstance(node, ast.Call)
        and (isinstance(node.func, ast.Attribute) and node.func.attr in {"include_router", "mount", "add_api_route", "add_route"}
             or isinstance(node.func, ast.Name) and node.func.id in {"FastAPI", "APIRouter"})])
    return routes, files


def calls(nodes):
    return {node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for root in nodes for node in ast.walk(root) if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))}


def callback_calls(callback, seen=None):
    seen = seen if seen is not None else set()
    nested = set()
    while isinstance(callback, partial):
        for argument in (*callback.args, *(callback.keywords or {}).values()):
            nested.update(callback_calls(argument, seen))
        callback = callback.func
    if not inspect.isfunction(callback) or callback in seen or not callback.__module__.startswith("app.services."):
        return nested
    seen.add(callback)
    tree = ast.parse(dedent(inspect.getsource(callback)))
    result = {callback.__name__} | calls([tree]) | nested
    for name in tuple(result):
        candidate = callback.__globals__.get(name)
        if inspect.isfunction(candidate):
            result.update(callback_calls(candidate, seen))
    return result


def validate(routes, files, manifest):
    errors = []
    expected = manifest["routes"]
    for identity in sorted(routes.keys() - expected.keys()):
        errors.append(f"NEW route without reviewed Hub contract: {identity}")
    for identity in sorted(expected.keys() - routes.keys()):
        errors.append(f"Removed/renamed route still in ledger: {identity}")
    for filename in sorted(files.keys() | manifest["files"].keys()):
        if files.get(filename) != manifest["files"].get(filename):
            errors.append(f"Changed API wiring/imports, review required: {filename}")
    for identity in sorted(routes.keys() & expected.keys()):
        entry, actual = expected[identity], routes[identity]
        if entry.get("state") not in STATES or not entry.get("reason"):
            errors.append(f"Missing classification/reason: {identity}")
        if entry.get("state") == "pending" and entry.get("migration_group") not in PENDING_GROUPS:
            errors.append(f"Pending route without specific migration group: {identity}")
        if entry.get("state") == "out_of_scope":
            exclusion = manifest.get("scope_exclusions", {}).get(entry.get("scope_exclusion"))
            if not exclusion or not exclusion.get("reason"):
                errors.append(f"Excluded route without explicit scope decision: {identity}")
            if entry.get("contracts") or entry.get("boundary"):
                errors.append(f"Excluded route cannot count as shared coverage: {identity}")
        if entry.get("fingerprint") != actual["fingerprint"]:
            errors.append(f"Changed route/helper, contract review required: {identity}")
        if entry.get("state") == "shared":
            if not entry.get("contracts") or not entry.get("boundary"):
                errors.append(f"Shared route without catalog/boundary: {identity}")
            if not set(entry.get("boundary", [])) <= calls(actual["nodes"]):
                errors.append(f"Shared boundary bypassed: {identity}")
    return errors


def validate_catalog(routes, manifest):
    from app.services.hub_operations import get_operation, hub_queries
    queries = {query.key: query for query in hub_queries()}
    errors = []
    for identity, entry in manifest["routes"].items():
        if entry["state"] != "shared" or identity not in routes:
            continue
        for key in entry["contracts"]:
            contract = queries.get(key) or get_operation(key)
            if contract is None:
                errors.append(f"Unknown catalog key {key}: {identity}")
            elif entry.get("reader_method") and entry["reader_method"] not in callback_calls(contract.execute):
                errors.append(f"Catalog no longer uses shared reader {entry['reader_method']}: {key}")
    return errors


def coverage_report(manifest):
    entries = tuple(manifest["routes"].values())
    states = Counter(row["state"] for row in entries)
    groups = Counter(row["migration_group"] for row in entries if row["state"] == "pending")
    lines = [f"HTTP routes: {len(entries)} (routes are not a count of user capabilities)"]
    lines.extend(f"  {state}: {states[state]}" for state in sorted(STATES))
    lines.append("Remaining migration groups:")
    lines.extend(f"  {group}: {count}" for group, count in sorted(groups.items()))
    lines.append("Scope exclusions are not completed migrations. UI/infrastructure is not agent functionality.")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    routes, files = inventory()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if sys.argv[1:] == ["--report"]:
        print(coverage_report(manifest))
    elif len(sys.argv) == 3 and sys.argv[1] == "--describe":
        print(json.dumps({key: {"fingerprint": value["fingerprint"], "calls": sorted(calls(value["nodes"]))}
            for key, value in routes.items() if sys.argv[2] in key}, indent=2))
    else:
        errors = validate(routes, files, manifest) + validate_catalog(routes, manifest)
        if errors:
            print("\n".join(errors))
            raise SystemExit(1)
        print(f"Reviewed HTTP contracts verified: {len(routes)} routes. Pending exceptions remain migration work.")
