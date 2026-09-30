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
