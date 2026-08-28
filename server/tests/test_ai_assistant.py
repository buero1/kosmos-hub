from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.services.ai_assistant import HubAssistantService


def test_assistant_extracts_responses_output_text():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "First sentence."},
                    {"type": "output_text", "text": "Second sentence."},
                ],
            }
        ]
    }

    assert HubAssistantService._extract_output_text(payload) == "First sentence.\nSecond sentence."


def test_assistant_runs_a_validated_tool_call_before_returning_the_model_answer():
    service = object.__new__(HubAssistantService)
    captured_requests = []
    responses = iter(
        [
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_customer_search",
                        "name": "search_customers",
                        "arguments": '{"query":"lackiererei aschenbrunner"}',
                    }
                ]
            },
            {"output_text": "Für Autolackiererei Aschenbrenner sind zwei Updates offen."},
        ]
    )

    def fake_response(**kwargs):
        captured_requests.append(kwargs)
        return next(responses)

    service._create_openai_response = fake_response
    tools = _FakeTools()
    answer = service._run_tool_loop(
        api_key="test-key",
        model="test-model",
        question="Welche Updates von Lackiererei Aschenbrunner sind offen?",
        tools=tools,
    )

    assert answer == "Für Autolackiererei Aschenbrenner sind zwei Updates offen."
    assert tools.calls == [("search_customers", {"query": "lackiererei aschenbrunner"})]
    assert len(captured_requests) == 2
    continuation = captured_requests[1]["input_items"]
    assert continuation[-2]["type"] == "function_call"
    assert continuation[-2]["call_id"] == "call_customer_search"
    assert continuation[-1]["type"] == "function_call_output"
    assert '"Autolackiererei Aschenbrenner"' in continuation[-1]["output"]


def test_assistant_rejects_invalid_tool_arguments():
    service = object.__new__(HubAssistantService)

    try:
        service._parse_tool_arguments("not-json")
    except ValueError as exc:
        assert "invalid tool arguments" in str(exc)
    else:
        raise AssertionError("Invalid model arguments must not reach a Hub tool.")


def test_assistant_template_marks_only_the_actual_answer_for_browser_persistence():
    template = (Path(__file__).parents[1] / "app" / "templates" / "assistant.html").read_text(encoding="utf-8")

    assert '<section class="assistant-answer" data-assistant-result>\n        <p class="eyebrow">Assistant answer</p>' in template
    assert '<p class="eyebrow">OpenAI connection required</p>' in template
    assert 'data-assistant-result>\n        <p class="eyebrow">OpenAI connection required</p>' not in template


class _FakeTools:
    def __init__(self):
        self.state = SimpleNamespace(panel_scope="selected", panel_site_ids={7})
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return {
            "matches": [
                {
                    "id": 7,
                    "name": "Autolackiererei Aschenbrenner",
                    "zoho_status": "Aktuell",
                }
            ]
        }
