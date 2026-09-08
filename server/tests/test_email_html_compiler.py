from app.services.email_html_compiler import EMAIL_HTML_COMPILER_VERSION, EmailHtmlCompiler


def test_compiler_wraps_a_hub_fragment_in_a_neutral_compatible_document():
    compilation = EmailHtmlCompiler().compile('<p>Hallo <strong>Welt</strong></p>')

    assert compilation.source_html == '<p>Hallo <strong>Welt</strong></p>'
    assert compilation.version == EMAIL_HTML_COMPILER_VERSION
    assert compilation.compiled_html.startswith('<!doctype html>')
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in compilation.compiled_html
    assert 'class="hub-email-container"' not in compilation.compiled_html
    assert 'max-width: 640px' not in compilation.compiled_html
    assert '<table role="presentation" width="100%"' in compilation.compiled_html
    assert '<p style="margin: 0 0 10px; line-height: 110%; mso-line-height-rule: exactly">Hallo <strong>Welt</strong></p>' in compilation.compiled_html
    assert 'font-family: Verdana, Geneva, sans-serif; font-size: 12px; line-height: 110%; mso-line-height-rule: exactly; text-align: left;' in compilation.compiled_html


def test_compiler_can_be_temporarily_bypassed_for_a_delivery_comparison(monkeypatch):
    source = "<p>Direkter Vergleich</p>"
    monkeypatch.setenv("KOSMOS_EMAIL_HTML_COMPILER_BYPASS", "1")

    compilation = EmailHtmlCompiler().compile(source)

    assert compilation.compiled_html == source
    assert compilation.version.endswith("-bypassed")
    assert compilation.warnings == ("Der Hub-E-Mail-Compiler wurde für diesen Versand temporär umgangen.",)


def test_compiler_uses_the_persisted_editor_defaults_for_body_and_paragraphs():
    compilation = EmailHtmlCompiler().compile(
        "<p>Hallo</p><p>Welt</p>",
        font_family="Georgia, Times New Roman, serif",
        font_size=16,
        line_height=1.3,
    )

    assert 'font-family: Georgia, Times New Roman, serif; font-size: 16px; line-height: 130%; mso-line-height-rule: exactly; text-align: left;' in compilation.compiled_html
    assert compilation.compiled_html.count('margin: 0 0 10px; line-height: 130%; mso-line-height-rule: exactly') == 2


def test_compiler_keeps_only_the_body_from_a_full_document_and_makes_images_fluid():
    source = """<!doctype html><html><head><title>Ignored</title></head><body>
    <p><img src="cid:logo" width="480" style="display: block"> Logo</p>
    </body></html>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert "Ignored" not in compilation.compiled_html
    assert 'src="cid:logo"' in compilation.compiled_html
    assert "max-width: 100%" in compilation.compiled_html
    assert "height: auto" in compilation.compiled_html
    assert "border: 0" in compilation.compiled_html
    assert compilation.compiled_html.count("<!doctype html>") == 1


def test_compiler_inlines_safe_styles_keeps_explicit_mobile_stacking_and_adds_an_outlook_button():
    source = """<!doctype html><html><head><style>
    .cta { background-color: #297db8; color: #ffffff; padding: 12px 18px; text-decoration: none; }
    .columns td { width: 50%; }
    @media screen and (max-width: 600px) { .cta { display: block !important; width: 100% !important; } }
    .not-supported > p { color: #111111; }
    </style></head><body>
    <table class="columns"><tr><td class="hub-email-mobile-stack"><a class="cta" href="https://example.de">Mehr erfahren</a></td><td>Details</td></tr></table>
    </body></html>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert 'background-color: #297db8' in compilation.compiled_html
    assert 'class="hub-email-mobile-stack"' in compilation.compiled_html
    assert compilation.compiled_html.count('hub-email-mobile-stack') == 2
    assert '.cta { display: block !important; width: 100% !important; }' in compilation.compiled_html
    assert "!important !important" not in compilation.compiled_html
    assert '<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="https://example.de"' in compilation.compiled_html
    assert any("komplexe CSS-Auswahl" in warning for warning in compilation.warnings)


def test_compiler_turns_the_editor_button_markup_into_a_bulletproof_email_button():
    source = """<table role="presentation" width="100%"><tr><td align="center">
    <a class="hub-email-button" href="https://example.de/angebot" style="display: inline-block; background-color: #0e7c66; border-radius: 6px; color: #ffffff; padding: 13px 20px; text-decoration: none;">Zum Angebot</a>
    </td></tr></table>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert 'href="https://example.de/angebot"' in compilation.compiled_html
    assert "Zum Angebot" in compilation.compiled_html
    assert 'fillcolor="#0e7c66"' in compilation.compiled_html
    assert 'class="hub-email-button"' in compilation.compiled_html
    assert 'class="hub-email-mobile-stack"' not in compilation.compiled_html


def test_compiler_normalizes_rgb_button_colors_for_outlook_vml():
    source = """<a class="hub-email-button" href="https://example.de/angebot"
    style="background-color: rgb(255, 0, 0); color: rgb(255, 255, 255); padding: 13px 20px;">Zum Angebot</a>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert 'fillcolor="#ff0000"' in compilation.compiled_html
    assert 'style="color:#ffffff;font-family:Arial,Helvetica,sans-serif' in compilation.compiled_html


def test_compiler_adds_legacy_background_attributes_for_table_layouts():
    source = """<table style="background-color: rgb(207, 226, 243)"><tr>
    <td style="background-color: rgb(0, 0, 0)">Inhalt</td></tr></table>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert 'bgcolor="#cfe2f3"' in compilation.compiled_html
    assert 'bgcolor="#000000"' in compilation.compiled_html


def test_compiler_preserves_a_template_owned_full_width_background_and_inner_content_width():
    source = """<table width="100%" bgcolor="#cfe2f3"><tr><td align="center" style="padding: 30px 15px">
    <table width="600" style="max-width: 600px; background-color: #ffffff"><tr><td>Inhalt</td></tr></table>
    </td></tr></table>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert 'max-width: 640px' not in compilation.compiled_html
    assert 'bgcolor="#cfe2f3"' in compilation.compiled_html
    assert 'width="600"' in compilation.compiled_html
    assert 'padding: 30px 15px' in compilation.compiled_html


def test_compiler_makes_fixed_content_tables_fluid_and_keeps_their_outlook_width():
    source = """<table width="100%" bgcolor="#cfe2f3"><tr><td align="center">
    <table width="600" style="width: 600px; min-width: 600px; max-width: 600px"><tr><td>Inhalt</td></tr></table>
    </td></tr></table>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert '<!--[if mso]><table role="presentation" width="600"' in compilation.compiled_html
    assert 'class="hub-email-fluid-container"' in compilation.compiled_html
    assert 'width="100%"' in compilation.compiled_html
    assert 'width: 100%' in compilation.compiled_html
    assert 'max-width: 600px' in compilation.compiled_html
    assert 'min-width: 0' in compilation.compiled_html
    assert 'min-width: 600px' not in compilation.compiled_html
    assert '<!--[if mso]></td></tr></table><![endif]-->' in compilation.compiled_html


def test_compiler_makes_fixed_non_table_layout_elements_fluid_without_changing_cells_or_images():
    source = """<div style="width: 540px; min-width: 540px; background-color: #ffffff">
    <table width="100%"><tr><td width="300"><img src="cid:logo" width="82" alt="Logo"></td></tr></table>
    </div>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert 'class="hub-email-fluid-element"' in compilation.compiled_html
    assert 'max-width: 540px' in compilation.compiled_html
    assert 'width="300"' in compilation.compiled_html
    assert 'src="cid:logo" width="82"' in compilation.compiled_html
    assert '<!--[if mso]><table role="presentation" width="540"' not in compilation.compiled_html


def test_compiler_uses_outlook_safe_line_height_and_handles_layout_images():
    source = """<table><tr><td><img src="cid:logo" alt="Logo" width="82"></td></tr></table>
    <p style="line-height: 1.3">Hallo</p>"""

    compilation = EmailHtmlCompiler().compile(source)

    assert 'line-height: 130%' in compilation.compiled_html
    assert 'mso-line-height-rule: exactly' in compilation.compiled_html
    assert 'src="cid:logo" alt="Logo" width="82"' in compilation.compiled_html
    assert 'display: block' in compilation.compiled_html


def test_compiler_reports_markup_image_url_and_size_risks():
    source = "<p>Offen<div>verschachtelt</p>" + (
        '<p><img src="cid:without-width" alt="Ohne Breite">'
        + f'<a href="https://example.de/{"x" * 90}">Langer Link</a></p>'
        + "x" * (103 * 1024)
    )

    compilation = EmailHtmlCompiler().compile(source)

    assert any("nicht korrekt verschachtelt" in warning for warning in compilation.warnings)
    assert any("keine feste Breite" in warning for warning in compilation.warnings)
    assert any("sehr lange URL" in warning for warning in compilation.warnings)
    assert any("sehr groß" in warning for warning in compilation.warnings)


def test_compiler_adds_the_outlook_list_fallback_only_when_a_list_is_present():
    compilation = EmailHtmlCompiler().compile("<ul><li>Erster Punkt</li></ul>")

    assert "mso-special-format: bullet" in compilation.compiled_html


def test_compiler_keeps_only_safe_rules_when_a_template_stylesheet_is_saved():
    stylesheet = EmailHtmlCompiler.sanitize_stylesheet("""<style>
    .cta { background-color: #297db8; padding: 12px; }
    @media screen and (max-width: 600px) { .cta { width: 100%; } }
    .card > p { color: #111111; }
    .tracking { background-image: url(https://invalid.example/pixel.png); }
    </style>""")

    assert ".cta { background-color: #297db8; padding: 12px; }" in stylesheet
    assert "@media screen and (max-width: 600px)" in stylesheet
    assert ".card > p" not in stylesheet
    assert "background-image" not in stylesheet
