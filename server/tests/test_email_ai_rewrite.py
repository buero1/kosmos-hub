from types import SimpleNamespace

import pytest

from app.services.email_ai_rewrite import EmailAiRewriteError, EmailAiRewriteService


def test_email_ai_rewrite_normalizes_ai_html_to_hub_email_markup():
    normalized = EmailAiRewriteService._normalized_html(
        '<p style="font-family: Arial">Guten Tag <strong>Frau Beispiel</strong>,</p>'
        '<div style="line-height: 2">vielen Dank für Ihre Nachricht.</div>'
    )

    assert normalized == "Guten Tag <strong>Frau Beispiel</strong>,<br><br>vielen Dank für Ihre Nachricht."
    assert "<p" not in normalized
    assert "<div" not in normalized
    assert "Arial" not in normalized


def test_email_ai_rewrite_requests_only_a_replacement_for_the_selected_fragment(monkeypatch):
    service = EmailAiRewriteService(db=SimpleNamespace(), cipher=SimpleNamespace())
    config = SimpleNamespace(model="test-model")
    requests: list[dict[str, object]] = []
    successes: list[object] = []
    service.provider_service = SimpleNamespace(
        get_enabled_openai_api_key=lambda: (config, "test-key"),
        record_request_success=lambda provider_config: successes.append(provider_config),
        record_request_error=lambda provider_config, code: None,
    )
    monkeypatch.setattr(
        service,
        "_create_openai_response",
        lambda **kwargs: requests.append(kwargs) or {
            "output": [
                {
                    "type": "function_call",
                    "name": "return_email_rewrite",
                    "arguments": '{"replacement_html":"<p>Bitte melden Sie sich kurz.</p>"}',
                }
            ]
        },
    )

    proposal = service.rewrite(
        instruction="Formuliere professioneller",
        selected_html="<span>Bitte melden Sie sich.</span>",
    )

    assert proposal.replacement_html == "Bitte melden Sie sich kurz."
    assert successes == [config]
    assert requests == [
        {
            "api_key": "test-key",
            "model": "test-model",
            "instruction": "Formuliere professioneller",
            "selected_html": "<span>Bitte melden Sie sich.</span>",
        }
    ]


def test_email_ai_rewrite_rejects_an_empty_selection():
    with pytest.raises(EmailAiRewriteError, match="markiere zuerst einen Text"):
        EmailAiRewriteService._selection("<br>")
