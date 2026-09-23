import copy
import json
from types import SimpleNamespace

import pytest

from app.services import hub_operations as registry
from app.services import hub_query_selection as selection_module
from app.services.hub_agent import HubAgentService
from app.services.hub_agent_catalog import AgentCatalog
from app.services.hub_operations import HubOperationError, HubOperationInputField as Field, HubOperationService, HubQuery


@pytest.fixture
def source(monkeypatch):
    registry.hub_queries()
    rows, reads = [], []
    def read(service, values):
        if service.actor != "allowed":
            raise HubOperationError("Forbidden")
        reads.append(dict(values))
        selected = [row for row in rows if not values.get("native") or row["group"] == values["native"]]
        offset = int(values.get("offset") or 0)
        return {"items": selected[offset:offset + 25], "total": len(selected),
            "next_offset": str(offset + 25) if len(selected) > offset + 25 else None,
            "data_source": "Local saved data"}
    definition = HubQuery("future.records", "Future list", (Field("offset", "Offset"), Field("native", "Filter")), read)
    monkeypatch.setitem(registry._QUERIES, definition.key, definition)
    return SimpleNamespace(rows=rows, reads=reads, key=definition.key,
        gateway=HubOperationService(db=None, cipher=None, actor="allowed"), definition=definition, monkeypatch=monkeypatch)


def choose(source, options, values=None):
    return source.gateway.query(source.key, values or {}, selection=options)


def test_selects_all_global_minimum_ties_across_every_page_without_excess_columns(source):
    for index in range(128):
        source.rows.append({"id": str(index), "group": "A", "name": f"Record {index:03}",
            "version": "6.2.12" if index in (0, 70, 127) else "6.10.1", "unneeded": "x" * 400})
    result = choose(source, {"fields": ["id", "name", "version"], "extreme": {"field": "version", "type": "version", "kind": "min"}})
    assert [row["id"] for row in result["items"]] == ["0", "70", "127"]
    assert result["source_total"] == 128 and result["source_pages"] == 6 and result["complete_scan"]
    assert result["total"] == 3 and result["next_offset"] is None
    assert result["source_metadata"]["data_source"] == "Local saved data"
    assert len(json.dumps(result)) < len(json.dumps(source.rows)) * .1
    assert "unneeded" not in json.dumps(result)


def test_extreme_pagination_never_discards_equal_minimums(source):
    source.rows.extend({"id": str(index), "version": "1.0"} for index in range(28))
    options = {"fields": ["id"], "extreme": {"field": "version", "type": "version", "kind": "min"}}
    first = choose(source, options)
    last = choose(source, {**options, "offset": int(first["next_offset"])})
    assert first["total"] == last["total"] == 28
    assert len(first["items"]) == 25 and len(last["items"]) == 3 and last["next_offset"] is None


def test_native_filters_and_generic_filters_are_applied_before_extreme(source):
    source.rows.extend([
        {"group": "A", "customer": "", "version": "1.0"},
        {"group": "A", "customer": "Alpha", "version": "6.2.12"},
        {"group": "A", "customer": "Beta", "version": "6.9"},
        {"group": "B", "customer": "Other", "version": "0.1"},
    ])
    result = choose(source, {"filters": [{"field": "customer", "op": "not_empty"}],
        "extreme": {"field": "version", "type": "version", "kind": "min"}}, {"native": "A"})
    assert result["source_total"] == 3 and result["total"] == 1
    assert result["items"][0]["customer"] == "Alpha"
    assert source.reads[0]["native"] == "A"


def test_schema_probe_never_returns_record_values_or_fetches_later_pages(source):
    source.rows.extend({"id": str(index), "name": "PRIVATE_VALUE", "price": 2} for index in range(60))
    result = choose(source, {"schema_only": True})
    assert result["fields"] == {"id": "text", "name": "text", "price": "number"}
    assert len(source.reads) == 1 and "PRIVATE_VALUE" not in json.dumps(result)


def test_typed_sorting_and_invalid_values_are_explicit(source):
    source.rows.extend({"id": str(index), "version": version} for index, version in enumerate(("6.2.12", "6.2.3", "6.10", None, "unknown", "6.2.3.0")))
    result = choose(source, {"order_by": [{"field": "version", "type": "version"}]})
    assert [row["id"] for row in result["items"]] == ["1", "5", "0", "2", "3", "4"]
    assert result["invalid_or_missing_values"] == {"version": 2}
    minimum = choose(source, {"extreme": {"field": "version", "type": "version", "kind": "min"}})
    assert [row["id"] for row in minimum["items"]] == ["1", "5"]


def test_number_date_multicolumn_sort_and_count_only(source):
    source.rows.extend([
        {"price": "10", "date": "2026-01-01T02:00:00+02:00"},
        {"price": "2.5", "date": "2026-01-01T00:00:01Z"},
        {"price": "10", "date": "2026-01-01T00:00:02Z"},
    ])
    result = choose(source, {"order_by": [{"field": "price", "type": "number", "direction": "desc"}, {"field": "date", "type": "date", "direction": "desc"}]})
    assert [row["date"] for row in result["items"]] == [source.rows[2]["date"], source.rows[0]["date"], source.rows[1]["date"]]
    count = choose(source, {"filters": [{"field": "price", "type": "number", "op": "gt", "value": "3"}], "count_only": True})
    assert count["total"] == 2 and count["items"] == [] and count["next_offset"] is None


@pytest.mark.parametrize("op,value,expected", [("eq", "a", [0]), ("ne", "a", [1, 2, 3]),
    ("contains", "B", [1, 2]), ("in", ["a", "abc"], [0, 2]), ("is_empty", None, [3])])
def test_text_filters(op, value, expected, source):
    source.rows.extend({"id": index, "name": name} for index, name in enumerate(("A", "B", "Abc", "")))
    result = choose(source, {"filters": [{"field": "name", "op": op, "value": value}]})
    assert [row["id"] for row in result["items"]] == expected


@pytest.mark.parametrize("options", [[], {"sql": "SELECT *"}, {"fields": ["name; DROP TABLE users"]},
    {"fields": []}, {"limit": True}, {"offset": -1}, {"limit": 101},
    {"filters": [{"field": "name", "op": "execute", "value": "x"}]},
    {"filters": [{"field": "name", "op": "in", "value": "x"}]},
    {"filters": [{"field": "name", "op": "eq", "type": "number", "value": "NaN"}]},
    {"schema_only": True, "fields": ["name"]}, {"order_by": [{"field": "name", "direction": "sideways"}]}])
def test_bad_selection_fails_before_any_source_read(source, options):
    with pytest.raises(HubOperationError):
        choose(source, options)
    assert not source.reads


def test_unknown_field_and_nonzero_source_offset_fail_closed(source):
    source.rows.append({"name": "Visible"})
    with pytest.raises(HubOperationError, match="Unbekannte Spalte"):
        choose(source, {"fields": ["password"]})
    with pytest.raises(HubOperationError, match="offset 0"):
        choose(source, {}, {"offset": "25"})


@pytest.mark.parametrize("fault", ["missing_cursor", "stuck_cursor", "total_mismatch", "changed_total", "permission", "rows", "bytes", "time"])
def test_incomplete_or_unauthorized_sources_never_return_a_partial_minimum(source, fault):
    source.rows.extend({"id": index, "name": "a"} for index in range(30))
    original = source.definition.execute
    def reader(service, values):
        result = original(service, values)
        later = int(values["offset"]) > 0
        if fault == "missing_cursor": result.pop("next_offset")
        if fault == "stuck_cursor": result["next_offset"] = "0"
        if fault == "total_mismatch": result["total"] = 200
        if fault == "changed_total" and later: result["total"] = 29
        if fault == "permission" and later: raise HubOperationError("Forbidden")
        return result
    source.monkeypatch.setitem(registry._QUERIES, source.key, HubQuery(source.key, "List", source.definition.input_fields, reader))
    if fault == "rows": source.monkeypatch.setattr(selection_module, "MAX_ROWS", 26)
    if fault == "bytes": source.monkeypatch.setattr(selection_module, "MAX_SOURCE_CHARS", 100)
    if fault == "time":
        times = iter([0, 0, 31])
        source.monkeypatch.setattr(selection_module, "monotonic", lambda: next(times))
    with pytest.raises(HubOperationError):
        choose(source, {"extreme": {"field": "id", "kind": "min", "type": "number"}})


def test_empty_and_truncated_lists_do_not_claim_complete_text_analysis(source):
    assert choose(source, {"fields": ["name"]})["total"] == 0
    source.rows.append({"content": "Partial text", "next_text_offset": "600"})
    with pytest.raises(HubOperationError, match="Gekuerzte"):
        choose(source, {"filters": [{"field": "content", "op": "contains", "value": "text"}]})


def test_shared_selection_is_discoverable_and_agent_dedup_includes_projection(source):
    source.rows.append({"id": "1", "name": "Name"})
    catalog = AgentCatalog()
    assert "hub_read.select" in catalog.describe([source.key])["definitions"][0]["selection"]
    for fields in (["id"], ["name"]):
        args = {"queries": [{"key": source.key, "input": {}, "select": {"fields": fields}}]}
        first = catalog.execute("hub_read", args, source.gateway)
        assert set(first["results"][0]["data"]["items"][0]) == set(fields)
        assert catalog.execute("hub_read", args, source.gateway)["results"][0]["already_read"]
    assert len(source.reads) == 2


def test_growing_tool_results_keep_explicit_cache_boundaries_and_reasoning(source, monkeypatch):
    agent = HubAgentService(db=None, cipher=None)
    captured = []
    def respond(**kwargs):
        payload = kwargs["payload"]
        captured.append(copy.deepcopy(payload))
        index = len(captured)
        name = "propose_hub_actions" if index == 7 else "hub_catalog_search"
        arguments = {"summary": "Done", "response": "Done", "actions": []} if index == 7 else {"query": "future", "offset": 0}
        return {"output": [{"type": "reasoning", "id": f"rs-{index}", "encrypted_content": "opaque", "summary": []},
            {"type": "function_call", "call_id": str(index), "name": name, "arguments": json.dumps(arguments)}]}
    monkeypatch.setattr(agent, "_create_openai_response", respond)
    agent._create_plan(api_key="not-used", model="gpt-5.6-sol", actor="allowed", instruction="Test")
    for before, after in zip(captured, captured[1:]):
        assert after["input"][:len(before["input"])] == before["input"]
        result = after["input"][-1]
        assert result["type"] == "function_call_output"
        assert result["output"][0]["type"] == "input_text"
        assert result["output"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert json.loads(result["output"][0]["text"])["untrusted_source_data"]
        assert after["input"][-3]["type"] == "reasoning"
        assert before["prompt_cache_key"] == after["prompt_cache_key"]
    first_key = captured[0]["prompt_cache_key"]
    captured.clear()
    agent._create_plan(api_key="not-used", model="gpt-5.6-sol", actor="another-user", instruction="Test")
    assert captured[0]["prompt_cache_key"] != first_key

