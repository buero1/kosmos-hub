import shutil
import subprocess
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base

from app.services import hub_operations as registry
from app.services.hub_agent import HubAgentService
from app.services.hub_operations import HubOperation, HubOperationInputField, HubOperationResult


def test_new_operation_supplies_agent_fields_defaults_preview_and_execution(monkeypatch):
    registry.agent_operations()
    calls = []

    def execute(service, values):
        calls.append((service.actor, dict(values)))
        return HubOperationResult("Open", "/example/23", 23, outputs={"example_id": "23"})

    operation = HubOperation(
        key="test.example.create", module="example", label="Example", description="Test operation",
        input_guide="", preview_fields=(("name", "Name"),), execute=execute,
        input_fields=lambda: (HubOperationInputField("name", "Name", required=True),),
        defaults=lambda values: {"name": "Module default"},
    )
    monkeypatch.setitem(registry._OPERATIONS, operation.key, operation)
    tool = HubAgentService._proposal_tool_definition()
    assert operation.key in tool["parameters"]["properties"]["actions"]["items"]["properties"]["action_type"]["enum"]
    plan = HubAgentService._normalize_plan({"summary": "Test", "response": "Test", "actions": [
        {"action_type": operation.key, "title": "Test", "details": "Test", "input": {}}
    ]})
    action = plan["actions"][0]
    assert action["input"] == {"name": "Module default"}
    assert HubAgentService._preview_lines(operation.key, action["input"]) == operation.preview(action["input"])
    service = object.__new__(HubAgentService)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    service.db = Session(engine)
    service.cipher = None
    result = service._execute_payload(action_type=operation.key, payload=action, actor="test-user")
    service.db.close()
    engine.dispose()
    assert calls == [("test-user", {"name": "Module default"})]
    assert result["outputs"] == {"example_id": "23"}


def test_rendered_base_javascript_still_parses_without_legacy_redirects():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for JavaScript syntax validation")
    from test_main_navigation import _render_base

    html = _render_base("/customers/117", admin=True)
    scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", html)
    assert scripts
    for source in scripts:
        if not source.strip():
            continue
        result = subprocess.run([node, "--check", "-"], input=source, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
