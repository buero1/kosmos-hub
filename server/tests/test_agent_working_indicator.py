from pathlib import Path
import re


BASE_TEMPLATE = Path(__file__).resolve().parents[1] / "app/templates/base.html"


def test_working_indicator_is_accessible_and_outside_replaced_messages():
    source = BASE_TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r'<div[^>]*data-agent-float-messages[^>]*></div>\s*'
                     r'<div[^>]*data-agent-float-working[^>]*role="status"[^>]*hidden>', source)
    assert '<span class="agent-working-dots" aria-hidden="true"><span></span><span></span><span></span></span>' in source
    assert '.agent-float-working[hidden] { display: none; }' in source
    assert '@media (prefers-reduced-motion: reduce)' in source
    assert '.agent-working-dots span { animation: none; opacity: 1; }' in source


def test_all_agent_requests_clear_busy_state_in_finally():
    source = BASE_TEMPLATE.read_text(encoding="utf-8")
    script = source.split('(function setupHubAgentFloat() {', 1)[1]
    assert 'working.hidden = !busy;' in script
    assert 'messages.setAttribute("aria-busy", busy ? "true" : "false");' in script
    assert 'send.disabled = input.disabled;' in script
    assert script.count('setBusy(false);') == 3
    assert len(re.findall(r'finally\s*\{\s*(?:upload.value = "";\s*)?setBusy\(false\);', script)) == 3
    for name in ('loadChat(conversationId)', 'postChat(path, values)'):
        assert f'async function {name} {{\n            if (state.busy) {{ return; }}' in script
