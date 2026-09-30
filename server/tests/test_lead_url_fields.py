"""Lead URL fields share customer URL safety and preserve layout/edit values."""

from html.parser import HTMLParser
from pathlib import Path
import re
from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined
import pytest

from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS
from app.services.hub_leads import HubLeadFieldValue, HubLeadService


SOURCE = Path("app/templates/lead_detail.html").read_text(encoding="utf-8")
MACRO = re.search(r"{% macro lead_field_value\(field\) %}.*?{% endmacro %}", SOURCE, re.S).group()


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)

    @property
    def links(self):
        return [attrs for tag, attrs in self.tags if tag == "a"]


def field(value, display_type="URL"):
    return HubLeadFieldValue(key="website", label="Webseite", display_type=display_type,
                             value=value, form_value=value)


def render(value):
    return Environment(autoescape=True, undefined=StrictUndefined).from_string(
        MACRO + "{{ lead_field_value(field) }}").render(field=value)


@pytest.mark.parametrize("value, href", [
    ("http://heissel.de/index.php?site=Home", "http://heissel.de/index.php?site=Home"),
    ("https://example.test/?a=1&b=2#part", "https://example.test/?a=1&b=2#part"),
    ("www.example.test/path", "https://www.example.test/path"),
    ("//example.test/path", "https://example.test/path"),
    ("example.test", "https://example.test"),
])
def test_lead_urls_open_in_safe_new_tab(value, href):
    markup = Markup(render(field(value)))
    assert markup.links == [{"href": href, "target": "_blank", "rel": "noopener noreferrer"}]
    assert "".join(markup.text).strip() == value


@pytest.mark.parametrize("value", [
    "", "-", "not a url", "javascript:alert(1)", "data:text/html,test", "file:///tmp/example",
    "https://", "https://[broken", "https://example.test:bad", "https://user:pass@example.test",
    "java\nscript:alert(1)", "https://example.test\\@other.test", '<img src=x onerror="alert(1)">',
])
def test_missing_or_unsafe_urls_remain_escaped_text(value):
    markup = Markup(render(field(value)))
    assert not markup.tags
    assert "".join(markup.text).strip() == (value or "-")


def test_url_attribute_and_label_are_escaped():
    value = 'https://example.test/?q="<svg/onload=alert(1)>'
    markup = Markup(render(field(value)))
    assert [tag for tag, _ in markup.tags] == ["a"]
    assert markup.links[0]["href"] == value
    assert set(markup.links[0]) == {"href", "target", "rel"}
    assert "".join(markup.text).strip() == value


def test_non_url_text_is_not_linkified():
    assert not Markup(render(field("https://example.test", "Einzelzeile"))).links


@pytest.mark.parametrize("show_more_index", [0, 1, 2])
def test_summary_expanded_and_layout_views_keep_field_order(show_more_index):
    start = SOURCE.index('<dl class="customer-fields-grid customer-fields-grid-summary">')
    end = SOURCE.index('<form id="lead-fields-layout-form"', start)
    fields = (field("https://example.test"), HubLeadFieldValue(
        key="company", label="Firma", display_type="Einzelzeile", value="Example", form_value="Example"))
    env = Environment(autoescape=True, undefined=StrictUndefined)
    html = env.from_string(MACRO + SOURCE[start:end]).render(
        detail=SimpleNamespace(fields=fields, show_more_index=show_more_index), show_more_layout_item=lambda: "")
    markup = Markup(html)
    assert len(markup.links) == 2  # Read-only section and layout preview.
    assert [attrs["data-layout-item-key"] for _, attrs in markup.tags if "data-layout-item-key" in attrs] == ["website", "company"]
    assert [text.strip() for text in markup.text if text.strip() in {"Webseite", "Firma"}] == ["Webseite", "Firma", "Webseite", "Firma"]


def test_catalog_url_field_retains_unmodified_edit_value():
    definition = next(item for item in HUB_LEAD_FIELDS if item.key == "website")
    value = "http://heissel.de/index.php?site=Home"
    result = HubLeadService(db=None, cipher=None)._field_value(definition, value)
    assert result.url_href == value
    assert result.form_value == value
    assert result.value == value
