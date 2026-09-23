import re

import pytest

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
    source = """<a class="hub-email-button" href="https://example.de/angebot"
    style="display: inline-block; background-color: #0e7c66; border-radius: 6px; color: #ffffff; padding: 13px 20px; text-decoration: none;">Zum Angebot</a>"""

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
    assert 'style="color:#ffffff;font-family:Verdana, Geneva, sans-serif' in compilation.compiled_html


def test_outlook_button_respects_appointment_template_dimensions_without_changing_modern_button():
    source = ('<td style="font-family: Arial, Helvetica, sans-serif">'
              '<a class="hub-email-button" href="https://example.de/" style="display: inline-block; '
              'background-color: rgb(255, 0, 0); color: rgb(255, 255, 255); font-size: 12px; '
              'font-weight: 700; line-height: 1; border-radius: 50px; padding: 10px 15px; '
              'text-decoration: none">Zu Preisen, Referenzen und mehr &gt;&gt;</a></td>')

    html = EmailHtmlCompiler().compile(source).compiled_html

    assert 'height:32px;v-text-anchor:middle;mso-wrap-style:none;' in html
    assert 'arcsize="100%"' in html
    assert 'inset="10.3137px,5.31371px,10.3137px,5.31371px"' in html
    assert 'font-family:Arial, Helvetica, sans-serif;font-size:12px;font-weight:700;' in html
    assert 'line-height:12px;mso-line-height-rule:exactly' in html
    assert 'mso-fit-shape-to-text:f;' in html
    assert 'mso-fit-shape-to-text:t;' not in html
    assert '<p align="center" style="text-align:center;margin:0;padding:0;' in html
    assert '<span style="color:#ffffff;' in html
    assert 'mso-text-fill-color:#ffffff;' in html
    assert 'height:42px' not in html
    assert 'width:316px' not in html
    modern = re.search(r'<!--\[if !mso\]><!-- -->(.*?)<!--<!\[endif\]-->', html, re.S)[1]
    assert 'border-radius: 50px; padding: 10px 15px' in modern
    assert 'font-size: 12px; font-weight: 700; line-height: 100%' in modern
    assert '<v:' not in modern
    assert modern.count('Zu Preisen, Referenzen und mehr &gt;&gt;') == 1


@pytest.mark.parametrize(('padding', 'inset', 'height'), [
    ('8px', '8px,8px,8px,8px', 30),
    ('8px 20px', '20px,8px,20px,8px', 30),
    ('8px 20px 12px', '20px,8px,20px,12px', 34),
    ('8px 20px 12px 24px', '24px,8px,20px,12px', 34),
    ('8px 20px; padding-left: 0; padding-bottom: 6pt', '0px,8px,20px,8px', 30),
    ('0.5em 1em', '14px,7px,14px,7px', 28),
])
def test_outlook_button_uses_authored_padding(padding, inset, height):
    html = EmailHtmlCompiler().compile(
        f'<a class="hub-email-button" href="https://example.de" '
        f'style="font-size:14px;line-height:1;padding:{padding}">Button</a>'
    ).compiled_html

    assert f'inset="{inset}"' in html
    assert f'height:{height}px;' in html
    assert 'mso-fit-shape-to-text:f;' in html


@pytest.mark.parametrize(('color', 'expected'), [
    ('#fff', '#ffffff'), ('rgb(255, 255, 255)', '#ffffff'),
    ('#2468ac', '#2468ac'), ('rgb(10, 20, 30)', '#0a141e'),
])
@pytest.mark.parametrize('button_attributes', [
    'class="hub-email-button"', 'class="btn"', '',
])
def test_outlook_buttons_apply_word_paragraph_alignment_and_text_run_color(color, expected, button_attributes):
    from xml.etree import ElementTree

    html = EmailHtmlCompiler().compile(
        '<div style="text-align:left;color:#000000;font-size:18px">'
        f'<a {button_attributes} href="https://example.de" '
        f'style="background:#125634;color:{color};font-size:12px;line-height:1;padding:10px 15px">'
        'A different button</a></div>'
    ).compiled_html
    shape_html = re.search(r'<v:roundrect\b.*?</v:roundrect>', html, re.S)[0]
    root = ElementTree.fromstring(
        '<root xmlns:w="urn:schemas-microsoft-com:office:word">' + shape_html + '</root>'
    )
    textbox = root.find('.//{urn:schemas-microsoft-com:vml}textbox')
    paragraph = textbox.find('p')
    run = paragraph.find('span')
    assert textbox.attrib['style'] == 'mso-fit-shape-to-text:f;'
    assert paragraph.attrib['align'] == 'center'
    assert 'text-align:center;' in paragraph.attrib['style']
    assert 'margin:0;' in paragraph.attrib['style']
    assert 'line-height:12px;' in paragraph.attrib['style']
    assert f'color:{expected};' in run.attrib['style']
    assert f'mso-text-fill-color:{expected};' in run.attrib['style']
    assert 'font-size:12px;' in run.attrib['style']
    assert run.text == 'A different button'
    assert '<div' not in shape_html
    modern = re.search(r'<!--\[if !mso\]><!-- -->(.*?)<!--<!\[endif\]-->', html, re.S)[1]
    assert f'color: {color}' in modern
    assert '<v:' not in modern


@pytest.mark.parametrize(('radius', 'arcsize'), [('0', '0'), ('4px', '25'), ('50%', '100'), ('99px', '100')])
def test_outlook_button_maps_css_radius_to_vml_half_dimension(radius, arcsize):
    html = EmailHtmlCompiler().compile(
        f'<a class="hub-email-button" style="font-size:12px;line-height:1;padding:10px 15px;'
        f'border-radius:{radius}">Button</a>'
    ).compiled_html

    assert f'arcsize="{arcsize}%"' in html
    inset = re.search(r'<v:textbox inset="([^"]+)"', html)[1]
    actual_insets = [float(value.removesuffix('px')) for value in inset.split(',')]
    corner_inset = (float(arcsize) / 100) * 16 * (1 - 2 ** -0.5)
    assert actual_insets == pytest.approx([15 - corner_inset, 10 - corner_inset] * 2, abs=0.0001)


def test_outlook_button_inherits_typeface_and_resets_after_parent_closes():
    html = EmailHtmlCompiler().compile(
        '<div style="font-family:Georgia, serif;font-size:16px;font-weight:bold;line-height:1.5">'
        '<a class="hub-email-button">First</a></div>'
        '<a class="hub-email-button">Second</a>',
        font_family="Courier New, monospace", font_size=14, line_height=1.2,
    ).compiled_html

    shapes = re.findall(r'<v:roundrect\b.*?</v:roundrect>', html, re.S)
    assert len(shapes) == 2
    assert 'font-family:Georgia, serif;font-size:16px;font-weight:bold;' in shapes[0]
    assert 'line-height:24px;' in shapes[0]
    assert 'font-family:Courier New, monospace;font-size:14px;font-weight:normal;' in shapes[1]
    assert 'line-height:16.8px;' in shapes[1]


@pytest.mark.parametrize(('box_sizing', 'width', 'height'), [('', 140, 50), ('border-box', 100, 30)])
def test_outlook_button_respects_explicit_dimensions_and_box_sizing(box_sizing, width, height):
    html = EmailHtmlCompiler().compile(
        f'<a class="hub-email-button" style="width:100px;height:30px;padding:10px 20px;'
        f'box-sizing:{box_sizing}">Button</a>'
    ).compiled_html

    assert f'height:{height}px;' in html
    assert f'width:{width}px;' in html


def test_outlook_button_escapes_text_links_and_typeface_without_breaking_attributes():
    html = EmailHtmlCompiler().compile(
        '<a class="hub-email-button" href="https://example.de/?a=1&amp;b=2" '
        'style="font-family: &#39;Courier New&#39;, monospace">&lt;Hello&gt; &amp; bye</a>'
    ).compiled_html

    shape = re.search(r'<v:roundrect\b.*?</v:roundrect>', html, re.S)[0]
    assert 'href="https://example.de/?a=1&amp;b=2"' in shape
    assert 'font-family:&#x27;Courier New&#x27;, monospace' in shape
    assert '&lt;Hello&gt; &amp; bye' in shape
    assert '<Hello>' not in shape


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
