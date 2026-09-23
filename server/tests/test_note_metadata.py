from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.templates import create_templates
from app.core.timezones import format_berlin_time_local


@pytest.mark.parametrize("value, expected", [
    (datetime(2026, 7, 28, 9, 30, 41, tzinfo=UTC), "28.07.2026 11:30:41"),
    (datetime(2026, 1, 28, 9, 30, 41), "28.01.2026 10:30:41"),
    ("2026-07-28T09:30:41Z", "28.07.2026 11:30:41"),
    ("2026-01-28T10:30:41+01:00", "28.01.2026 10:30:41"),
    (None, "-"),
    ("", "-"),
    ("unknown", "unknown"),
])
def test_note_time_keeps_local_time_and_seconds_without_zone(value, expected):
    assert format_berlin_time_local(value) == expected


@pytest.mark.parametrize("author", ["Sarah Mueller", "Team Kosmos", None])
def test_shared_note_metadata(author):
    templates = create_templates(directory="app/templates")
    html = templates.env.get_template("partials/note_metadata.html").render(
        note=SimpleNamespace(author=author, occurred_at=datetime(2026, 7, 28, 9, 30, 41, tzinfo=UTC)))
    assert (author or "Nicht bekannt") in html
    assert "28.07.2026 11:30:41" in html
    assert "CEST" not in html and "CET" not in html
    for name in ("lead_detail.html", "customer_detail.html"):
        assert '{% include "partials/note_metadata.html" %}' in Path("app/templates", name).read_text(encoding="utf-8")


def test_note_metadata_escapes_author():
    html = create_templates(directory="app/templates").env.get_template("partials/note_metadata.html").render(
        note=SimpleNamespace(author="<script>alert(1)</script>", occurred_at=None))
    assert "<script>" not in html and "&lt;script&gt;" in html
