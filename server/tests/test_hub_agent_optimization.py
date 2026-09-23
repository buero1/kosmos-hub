from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from app.services.hub_agent import HubAgentError, HubAgentService
from app.services.hub_agent_catalog import AgentCatalog
from app.services.hub_operations import HubOperation, HubOperationError, HubOperationInputField, HubQuery
from app.services import hub_operations as registry


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_small_initial_context_preserves_large_plan_capacity_and_entire_pdf(model):
    agent = object.__new__(HubAgentService)
    captured = []
    def response(**kwargs):
        captured.append(kwargs["payload"])
        return {"output": [{"type": "function_call", "name": "propose_hub_actions",
            "arguments": json.dumps({"summary": "Test", "response": "Test", "actions": []})}]}
    agent._create_openai_response = response
    agent._create_plan(api_key="test", model=model, instruction="Hallo")
    plain = captured[0]
    size = sum(len(json.dumps(plain[key], ensure_ascii=False)) for key in ("instructions", "tools", "input"))
    assert size < 20000
    print(f"BASELINE_CHARS={size}; BEFORE_REFERENCE=275171; REDUCTION={100*(1-size/275171):.1f}%")
    assert plain["reasoning"] == {"effort": "low"}
    assert plain["prompt_cache_options"] == {"mode": "explicit"}
    assert plain["input"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert plain["max_output_tokens"] == 12000
    assert "offer_field__payment_terms" not in plain["instructions"]
    document = "POSITION COMPLETE " * 12000
    agent._create_plan(api_key="test", model=model, instruction="Alle Positionen", additional_contexts=(document,))
    assert document in captured[-1]["input"][1]["content"][0]["text"]
    assert captured[-1]["input"][1]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}


def test_future_actions_and_queries_are_discoverable_without_agent_changes(monkeypatch):
    registry.agent_operations()
    operation = HubOperation(key="newmodule.create", module="newmodule", label="Spezialobjekt anlegen",
        description="Spezialobjekt verwalten", input_guide="", preview_fields=(), execute=lambda *args: None,
        input_fields=lambda: (HubOperationInputField("name", "Name", required=True),), defaults=lambda values: {"name": "Standard"})
    query = HubQuery("newmodule.read", "Spezialobjekt lesen", (), lambda *args: {"ok": True})
    monkeypatch.setitem(registry._OPERATIONS, operation.key, operation)
    monkeypatch.setitem(registry._QUERIES, query.key, query)
    catalog = AgentCatalog()
    assert {item["key"] for item in catalog.search("newmodule")["items"]} == {operation.key, query.key}
    definition = catalog.describe([operation.key])["definitions"][0]
    assert definition["fields"]["name"] == {"label": "Name", "required": True, "default": "Standard"}
    assert catalog.describe([query.key])["definitions"][0]["kind"] == "read"


def test_optional_fields_and_defaults_come_from_shared_module():
    definition = AgentCatalog().describe(["finance.offers.create"])["definitions"][0]
    assert definition["fields"]["offer_field__payment_terms"]["required"] is False
    assert definition["fields"]["offer_field__status"]["default"] == "draft"
    assert AgentCatalog().describe(["emails.send"])["definitions"][0].get("error")


def test_batch_reads_are_bounded_deduplicated_and_never_execute_mutations(monkeypatch):
    registry.hub_queries()
    query = HubQuery("test.read", "Read", (), lambda *args: {})
    monkeypatch.setitem(registry._QUERIES, query.key, query)
    calls = []
    gateway = SimpleNamespace(query=lambda key, values: calls.append((key, values)) or {"value": "OK"})
    catalog = AgentCatalog()
    arguments = {"queries": [{"key": query.key, "input": {}}, {"key": query.key, "input": {}}]}
    result = catalog.execute("hub_read", arguments, gateway)
    assert len(calls) == 1
    assert result["results"][1]["already_read"] is True
    assert result["untrusted_source_data"] is True
    for bad in ({"queries": [{"key": "leads.create", "input": {}}]}, {"queries": [{}] * 5}, {"queries": []}):
        with pytest.raises(HubOperationError): catalog.execute("hub_read", bad, gateway)


def test_oversized_results_are_not_cached_as_if_the_model_had_read_them(monkeypatch):
    registry.hub_queries()
    monkeypatch.setitem(registry._QUERIES, "test.large", HubQuery("test.large", "Read", (), lambda *args: {}))
    catalog = AgentCatalog()
    calls = []
    gateway = SimpleNamespace(query=lambda key, values: calls.append(key) or {"text": "x" * 80000})
    args = {"queries": [{"key": "test.large", "input": {}}]}
    for _ in range(2):
        assert "error" in catalog.execute("hub_read", args, gateway)["results"][0]
    assert len(calls) == 2


def test_cost_guard_stops_before_starting_another_model_request():
    agent = object.__new__(HubAgentService)
    agent.usage_trace = SimpleNamespace(estimated_upper=Decimal("0.51"))
    agent._create_openai_response = lambda **kwargs: pytest.fail("Budget exceeded")
    with pytest.raises(HubAgentError, match="Kostenschutzgrenze"):
        agent._create_plan(api_key="test", model="gpt-5.6-sol", instruction="Test")


def test_catalog_pages_allow_discovery_of_every_registered_capability():
    catalog = AgentCatalog()
    offset = 0
    found = set()
    while True:
        page = catalog.search(offset=offset)
        found.update(item["key"] for item in page["items"])
        if page["next_offset"] is None: break
        offset = page["next_offset"]
    assert found == set(catalog.operations) | set(catalog.queries)
