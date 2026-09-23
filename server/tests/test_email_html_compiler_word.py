"""Optional Word-engine regression test; RUN_WORD_EMAIL_TESTS=1 on Windows with Word installed."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services.email_html_compiler import EmailHtmlCompiler


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("RUN_WORD_EMAIL_TESTS") != "1",
    reason="Requires opt-in and local Microsoft Word; never sends mail.",
)


@pytest.mark.parametrize(("font", "font_size", "line_height", "padding", "radius", "color", "label"), [
    ("Verdana", 12, 1, (10, 15, 10, 15), 50, "#ffffff", "Zu Preisen, Referenzen und mehr >>"),
    ("Arial", 14, 1, (13, 20, 13, 20), 6, "#ffffff", "Zum Angebot"),
    ("Georgia", 16, 1.5, (8, 20, 12, 24), 4, "#2468ac", "Details & Kontakt"),
    ("Courier New", 12, 1.2, (0, 10, 0, 10), 0, "#ffffff", "Mehr erfahren"),
    ("Arial", 16, 1.2, (1, 4, 1, 4), 50, "#ffffff", "Kompakter Button"),
])
def test_word_renders_button_geometry_text_color_and_alignment(
    tmp_path, font, font_size, line_height, padding, radius, color, label,
):
    from html import escape

    source = (
        '<table width="600"><tr><td style="color:#000000;text-align:left">'
        '<a class="hub-email-button" href="https://example.invalid/" '
        f'style="display:inline-block;background:#ff0000;color:{color};font-family:{font};'
        f'font-size:{font_size}px;font-weight:700;line-height:{line_height};'
        f'padding:{" ".join(f"{value}px" for value in padding)};border-radius:{radius}px">'
        f'{escape(label)}</a></td></tr></table>'
    )
    fixture = tmp_path / "button.html"
    fixture.write_text(EmailHtmlCompiler().compile(source).compiled_html, encoding="utf-8")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(Path(__file__).parent / "support/inspect_word_email.ps1"),
         "-Path", str(fixture)],
        check=True, capture_output=True, encoding="utf-8-sig", timeout=90,
    )
    shapes = json.loads(result.stdout)
    assert len(shapes) == 1
    shape = shapes[0]
    # Word uses points and a BGR integer for RGB colors.
    assert shape["height"] == pytest.approx((font_size * line_height + padding[0] + padding[2]) * 0.75, abs=0.1)
    assert shape["font_size"] == pytest.approx(font_size * 0.75, abs=0.1)
    assert shape["line_spacing"] == pytest.approx(font_size * line_height * 0.75, abs=0.1)
    assert shape["alignment"] == 1
    assert shape["color"] == int.from_bytes(bytes.fromhex(color[1:]), "little")
    assert shape["text"] == label
    corner_inset = min(radius, shape['height'] / 0.75 / 2) * (1 - 2 ** -0.5)
    for side, value in zip(("top", "right", "bottom", "left"), padding):
        assert shape[side] + corner_inset * 0.75 == pytest.approx(value * 0.75, abs=0.1)
