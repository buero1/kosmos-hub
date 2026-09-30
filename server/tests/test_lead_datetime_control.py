from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment


def _render_datetime_control(value: str) -> str:
    source = Path("app/templates/partials/lead_field_control.html").read_text(
        encoding="utf-8"
    )
    macro = Environment(autoescape=True).from_string(source).module.lead_field_control
    field = SimpleNamespace(
        display_type="DatumZeit",
        label="Termindatum Beratungsgespräch",
        read_only=False,
        required=False,
    )
    return str(macro(field, "lead_field__appointment_at", value))


def test_lead_datetime_control_uses_half_hour_steps_without_seconds():
    rendered = _render_datetime_control("2026-09-30T12:30:45")

    assert 'type="datetime-local"' in rendered
    assert 'step="1800"' in rendered
    assert 'value="2026-09-30T12:30"' in rendered
    assert "12:30:45" not in rendered


def test_lead_industry_control_is_searchable_and_accepts_free_text():
    source = Path("app/templates/partials/lead_field_control.html").read_text(
        encoding="utf-8"
    )
    macro = Environment(autoescape=True).from_string(source).module.lead_field_control
    field = SimpleNamespace(
        key="industry",
        value="Autoreparaturen",
        display_type="Auswahlliste",
        label="Branche",
        read_only=False,
        required=False,
    )

    rendered = str(macro(field, "lead_field__industry", "Autoreparaturen", ("Automobile", "Autoreparaturen")))

    assert 'type="text"' in rendered
    assert 'list="lead-industry-options"' in rendered
    assert '<datalist id="lead-industry-options">' in rendered
    assert 'value="Automobile"' in rendered
    assert '<select' not in rendered
