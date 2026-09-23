from datetime import UTC, datetime
import json
from zoneinfo import ZoneInfo

import pytest

from app.core.timezones import berlin_datetime, format_berlin_time_local, iso_berlin_time
from app.services.hub_agent import HubAgentService
from app.services.hub_agent_catalog import AgentCatalog
from app.models.site_snapshot import SiteSnapshot
from app.models.site_update_snapshot import SiteUpdateSnapshot
from test_hub_website_operations import hub, add_site


@pytest.mark.parametrize("raw, expected", [
    ("2026-09-23T05:41:16", "2026-09-23T07:41:16+02:00"),
    ("2026-01-23T05:41:16", "2026-01-23T06:41:16+01:00"),
    ("2026-09-23T23:30:00", "2026-09-24T01:30:00+02:00"),
    ("2026-09-23T07:41:16+02:00", "2026-09-23T07:41:16+02:00"),
    ("2026-09-23T01:41:16-04:00", "2026-09-23T07:41:16+02:00"),
    ("2026-03-29T00:30:00", "2026-03-29T01:30:00+01:00"),
    ("2026-03-29T01:30:00", "2026-03-29T03:30:00+02:00"),
    ("2026-10-25T00:30:00", "2026-10-25T02:30:00+02:00"),
    ("2026-10-25T01:30:00", "2026-10-25T02:30:00+01:00"),
])
def test_agent_timestamps_use_same_berlin_time_as_hub_without_double_conversion(raw, expected):
    value = datetime.fromisoformat(raw)
    assert iso_berlin_time(value) == expected
    expected_display = datetime.fromisoformat(expected).strftime("%d.%m.%Y %H:%M:%S")
    assert HubAgentService._context_datetime(value) == format_berlin_time_local(value) == expected_display
    assert format_berlin_time_local(expected) == expected_display
    assert berlin_datetime(value).tzinfo == ZoneInfo("Europe/Berlin")


def test_missing_context_timestamp_is_not_invented():
    assert HubAgentService._context_datetime(None) == "-"


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_every_model_receives_hub_clock_and_display_rules(model):
    agent = object.__new__(HubAgentService)
    captured = []

    def response(**kwargs):
        captured.append(kwargs["payload"])
        return {"output": [{"type": "function_call", "name": "propose_hub_actions",
            "arguments": json.dumps({"summary": "Test", "response": "Test", "actions": []})}]}

    agent._create_openai_response = response
    before = datetime.now(UTC).astimezone(ZoneInfo("Europe/Berlin"))
    agent._create_plan(api_key="test", model=model, instruction="Read only")
    after = datetime.now(UTC).astimezone(ZoneInfo("Europe/Berlin"))
    instructions = captured[0]["instructions"]
    assert "Europe/Berlin" in instructions
    assert "TT.MM.JJJJ HH:MM:SS" in instructions
    assert "ohne CET/CEST" in instructions
    assert "Katalogformat" in instructions
    prompt = "\n".join(part["text"] for item in captured[0]["input"]
                       if item["role"] == "user" for part in item["content"])
    clock = prompt.split("HUB-ZEIT: ", 1)[1].split(" (Europe/Berlin)", 1)[0]
    parsed = datetime.strptime(clock, "%d.%m.%Y %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Berlin"))
    assert before.replace(microsecond=0) <= parsed <= after


def test_shared_website_read_and_agent_tool_use_local_offset_and_preserve_instant(hub):
    site = add_site(hub)
    stored = datetime(2026, 9, 23, 5, 41, 16)
    hub.db.add(SiteSnapshot(site_id=site.id, captured_at=stored, plugins_json=[], themes_json=[], environment_json={}))
    hub.db.add(SiteUpdateSnapshot(site_id=site.id, captured_at=stored, core_updates_json=[],
        plugin_updates_json=[], theme_updates_json=[], summary_json={}))
    hub.db.commit()
    queries = [{"key": key, "input": {"site_id": str(site.id)}} for key in
               ("websites.read", "websites.inventory", "wordpress.updates.read")]
    result = AgentCatalog().execute("hub_read", {"queries": queries}, hub)
    for row in result["results"]:
        data = row["data"]
        for key in ("captured_at", "inventory_captured_at", "updates_captured_at"):
            if key in data:
                assert data[key] == "2026-09-23T07:41:16+02:00"
                assert datetime.fromisoformat(data[key]).astimezone(UTC).replace(tzinfo=None) == stored
    assert not hub.db.dirty
