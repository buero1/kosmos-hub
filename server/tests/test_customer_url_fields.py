from html.parser import HTMLParser
from pathlib import Path
import re
from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined
import pytest

from app.services.customer_directory import CustomerProfileField


def render_field(field, *, site=None):
    source = Path('app/templates/customer_detail.html').read_text(encoding='utf-8')
    macro = re.search(r'{% macro customer_profile_field_value\(field\) %}.*?{% endmacro %}', source, re.S).group()
    env = Environment(autoescape=True, undefined=StrictUndefined)
    return env.from_string(macro + '{{ customer_profile_field_value(field) }}').render(
        field=field, detail=SimpleNamespace(wordpress_admin_site=site),
        wordpress_admin_quick_action=lambda site: 'wordpress-shortcut-' + str(site.id))


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


@pytest.mark.parametrize('key', ['website', 'work_domain_login', 'work_domain', 'previous_website', 'future_url_field'])
@pytest.mark.parametrize('value, href', [
    ('https://example.test/wp-admin', 'https://example.test/wp-admin'),
    ('http://example.test', 'http://example.test'),
    ('www.example.test/path', 'https://www.example.test/path'),
    ('//example.test/path', 'https://example.test/path'),
    ('https://example.test/?a=1&b=2', 'https://example.test/?a=1&b=2'),
])
def test_url_fields_are_safe_new_tab_links(key, value, href):
    field = CustomerProfileField(label='URL', key=key, value=value, display_type='URL')
    links = Links(render_field(field)).links
    assert len(links) == 1
    assert links[0]['href'] == href
    assert links[0]['target'] == '_blank'
    assert set(links[0]['rel'].split()) == {'noopener', 'noreferrer'}


@pytest.mark.parametrize('value', [None, '', 'javascript:alert(1)', 'data:text/html,test', 'file:///tmp/example',
    'https://', 'https://[broken', 'https://example.test:bad', 'https://user:password@example.test',
    'java\nscript:alert(1)', 'https://example.test\\@other.test', '-', 'not a url'])
def test_empty_or_unsafe_values_are_not_links(value):
    field = CustomerProfileField(label='URL', key='work_domain', value=value, display_type='URL')
    html = render_field(field)
    assert not Links(html).links
    if not value:
        assert html.strip() == '\u2013'


def test_wordpress_shortcut_is_retained_beside_login_link():
    field = CustomerProfileField(label='Login', key='work_domain_login', value='https://example.test/wp-admin', display_type='URL')
    html = render_field(field, site=SimpleNamespace(id=1))
    assert 'wordpress-shortcut-1' in html
    assert len(Links(html).links) == 1


def test_text_and_sensitive_fields_are_not_linkified():
    assert not Links(render_field(CustomerProfileField(label='Text', value='https://example.test'))).links
    assert not Links(render_field(CustomerProfileField(label='Private', value='https://example.test', display_type='URL', sensitive=True))).links
