from pathlib import Path
import re

import pytest
from jinja2 import Environment, FileSystemLoader


TEMPLATES = Path(__file__).resolve().parents[1] / "app/templates"
CHANGED = {
    "contact_create.html": 1,
    "customer_contact_detail.html": 1,
    "customer_detail.html": 5,
    "emails.html": 3,
    "lead_detail.html": 2,
    "partials/customer_activity_composer.html": 1,
    "partials/lead_activity_panel.html": 1,
}


@pytest.mark.parametrize("filename, expected", CHANGED.items())
def test_module_headings_use_hub_brand_and_templates_parse(filename, expected):
    source = (TEMPLATES / filename).read_text(encoding="utf-8")
    assert source.count('<p class="eyebrow">Kosmos Hub</p>') >= expected
    Environment(loader=FileSystemLoader(TEMPLATES)).parse(source)


def test_no_module_eyebrow_retains_zoho_brand():
    for path in TEMPLATES.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        headings = re.findall(r'<p\b[^>]*class="[^"]*\beyebrow\b[^"]*"[^>]*>(.*?)</p>', source, re.S)
        assert not any("zoho crm" in value.lower() for value in headings), path


def test_connector_settings_remain_separate_from_neutral_record_panels():
    customer = (TEMPLATES / "customer_detail.html").read_text(encoding="utf-8")
    settings = (TEMPLATES / "account.html").read_text(encoding="utf-8")
    assert 'hub_data_panel(detail.entry.customer)' in customer
    assert '<dt>Quelle</dt>' not in customer
    assert '<h3>Zoho CRM</h3>' in settings
    assert 'href="/account/zoho/connect"' in settings
