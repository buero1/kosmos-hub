from dataclasses import replace

import pytest
from lxml import html

from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_pdf_templates import HubPdfTemplateService
from test_pdf_currency_display import snapshot_for
from test_pdf_totals_visibility import create, hub, render_settings


KINDS = ("offers", "orders", "invoices", "dunnings")


def render(kind, description, *, separate=False, without_name=False):
    snapshot = snapshot_for(kind)
    snapshot.lines[0].description = description
    content = HubPdfTemplateService._default_content(kind)
    for column in content["positions"]["columns"]:
        if column["source_key"] == "article_description":
            column["is_enabled"] = separate
        if without_name and column["source_key"] == "article_name":
            column["is_enabled"] = False
    source = HubFinancePdfService(db=None, cipher=None)._render_html(snapshot=snapshot, content=content)
    return html.fromstring(source)


@pytest.mark.parametrize("kind", KINDS)
def test_description_is_below_name_in_same_position_cell(kind):
    description = "Konzeption und Umsetzung\nInklusive Einrichtung & Einweisung"
    doc = render(kind, description)
    name = doc.xpath('//div[@class="pdf-position-name"]')[0]
    block = name.getnext()
    assert block.get("class") == "pdf-position-description"
    assert block.text_content() == description
    assert name.getparent() is block.getparent()
    assert len(doc.xpath('//div[@class="pdf-position-description"]')) == 1
    assert name.text_content() == "Website"
    assert "100,00" in name.getparent().getparent().text_content()
    assert "white-space: pre-line" in doc.xpath("//style")[0].text_content()


@pytest.mark.parametrize("description", ["", "  \r\n  ", None])
def test_empty_description_does_not_add_blank_paragraph(description):
    doc = render("offers", description)
    assert not doc.xpath('//div[@class="pdf-position-description"]')


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("without_name", [False, True])
def test_explicit_description_column_is_respected_without_duplicate(kind, without_name):
    doc = render(kind, "Standalone description", separate=True, without_name=without_name)
    descriptions = doc.xpath('//div[@class="pdf-position-description"]')
    assert len(descriptions) == 1
    assert descriptions[0].text_content() == "Standalone description"
    assert not descriptions[0].getparent().xpath('./div[@class="pdf-position-name"]')


def test_description_is_escaped_not_treated_as_html_or_template():
    description = '<script>alert(1)</script> & <img src="file:///etc/passwd">\n${Company.Name}'
    doc = render("offers", description)
    node = doc.xpath('//div[@class="pdf-position-description"]')[0]
    assert node.text_content() == description
    assert not node.xpath(".//*")
    assert not doc.xpath("//script | //img")


@pytest.mark.parametrize("kind", KINDS)
def test_template_preview_uses_same_combined_cell(hub, kind):
    template_id = create(hub, kind)
    doc = html.fromstring(render_settings(hub, template_id, kind))
    cells = doc.xpath('//*[@data-pdf-template-edit-positions]//tbody/tr/td[div[@class="pdf-position-name"]]')
    assert len(cells) == 4
    assert all(cell.xpath('./div[@class="pdf-position-description"]') for cell in cells)
    assert all("Website-Paket und laufende Betreuung" in cell.text_content() for cell in cells)


def test_descriptions_stay_with_their_own_position():
    snapshot = snapshot_for("offers")
    first = snapshot.lines[0]
    from types import SimpleNamespace
    second = SimpleNamespace(**(vars(first) | {"name": "Support", "description": "Laufende Wartung"}))
    snapshot = replace(snapshot, lines=(first, second))
    doc = html.fromstring(HubFinancePdfService(db=None, cipher=None)._render_html(
        snapshot=snapshot, content=HubPdfTemplateService._default_content("offers")))
    rows = doc.xpath('//table[@class="positions"]/tbody/tr')
    assert len(rows) == 2
    assert "Testposition" in rows[0].text_content() and "Laufende Wartung" not in rows[0].text_content()
    assert "Laufende Wartung" in rows[1].text_content() and "Testposition" not in rows[1].text_content()
