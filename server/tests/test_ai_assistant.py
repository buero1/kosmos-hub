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
