from pathlib import Path
import re


BASE_TEMPLATE = Path(__file__).resolve().parents[1] / "app/templates/base.html"


def _css_rule(selector: str) -> str:
    source = BASE_TEMPLATE.read_text(encoding="utf-8")
    match = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", source)
    assert match is not None, f"Missing CSS rule: {selector}"
    return match.group(1)


def test_attachment_links_use_medium_weight():
    assert "font-weight: 500;" in _css_rule(".communication-attachments a")


def test_email_preview_has_a_nonshrinking_stateful_disclosure_arrow():
    selector = ".communication-content-with-actions > summary"
    summary = _css_rule(selector)
    assert "display: flex;" in summary
    assert "gap: 0.5rem;" in summary
    assert "list-style: none;" in summary
    assert "display: none;" in _css_rule(selector + "::-webkit-details-marker")

    closed_arrow = _css_rule(selector + "::before")
    assert 'content: "";' in closed_arrow
    assert "flex: 0 0 auto;" in closed_arrow
    assert "border-right: 2px solid currentColor;" in closed_arrow
    assert "border-bottom: 2px solid currentColor;" in closed_arrow
    assert "transform: rotate(-45deg);" in closed_arrow
    assert "transform: rotate(45deg);" in _css_rule(
        ".communication-content-with-actions[open] > summary::before"
    )
