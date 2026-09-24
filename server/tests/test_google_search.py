"""Detail-menu searches use only company and address, never private CRM data."""

import json
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.services.customer_directory import CustomerDirectoryService
from app.services.google_search import business_google_search_url
from app.services.hub_leads import HubLeadService


def query(url):
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "www.google.com", "/search")
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    assert set(parameters) == {"q"}
    return parameters["q"][0]


@pytest.mark.parametrize(("parts", "expected"), [
    (("  Example  GmbH ", " Main Street\n12a ", "01234", "City"), "Example GmbH Main Street 12a 01234 City"),
    (("Example", None, "", "  "), "Example"),
    ((None, "", " "), ""),
    (("B\u00e4ckerei & S\u00f6hne + Partner", "Stra\u00dfe 12/1", "80331", "M\u00fcnchen"),
     "B\u00e4ckerei & S\u00f6hne + Partner Stra\u00dfe 12/1 80331 M\u00fcnchen"),
    (("\"<script>alert(1)</script>&q=other#fragment",), "\"<script>alert(1)</script>&q=other#fragment"),
])
def test_search_encodes_one_query_and_omits_missing_parts(parts, expected):
    assert query(business_google_search_url(*parts)) == expected


@pytest.fixture
def services():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        yield db, cipher, CustomerDirectoryService(db=db, cipher=cipher), HubLeadService(db=db, cipher=cipher)
    engine.dispose()


@pytest.mark.parametrize("imported", [False, True])
def test_customer_search_uses_canonical_address_for_local_and_imported_profiles(services, imported):
    db, cipher, customers, _ = services
    if imported:
        customer = Customer(name="Example GmbH", zoho_id="example-google-search", encrypted_profile_json=cipher.encrypt(json.dumps({
            "fields": {"Rechnungsadresse - Stra\u00dfe": "Main Street 12a", "Rechnungsadresse - PLZ": "01234",
                       "Rechnungsadresse - Stadt": "City", "IBAN": "PRIVATE", "Beschreibung": "PRIVATE"},
        })))
        db.add(customer)
        db.flush()
    else:
        customer = customers.create_hub_customer(submitted_values={
            "customer_field__customer_name": "Example GmbH", "customer_field__account_status": "Neu",
            "customer_field__billing_street": "Main Street 12a", "customer_field__billing_postal_code": "01234",
            "customer_field__billing_city": "City",
        })
    detail = customers.get_detail(customer_id=customer.id)
    assert query(detail.google_search_url) == "Example GmbH Main Street 12a 01234 City"
    assert query(customers.get_detail(customer_id=customer.id, include_sensitive=True).google_search_url) == query(detail.google_search_url)


def test_customer_search_without_profile_uses_customer_name(services):
    db, _, customers, _ = services
    customer = Customer(name="Example GmbH")
    db.add(customer)
    db.flush()
    assert query(customers.get_detail(customer_id=customer.id).google_search_url) == "Example GmbH"


@pytest.mark.parametrize("company", ["Example GmbH", "", "   "])
def test_lead_search_prefers_company_and_falls_back_to_name(services, company):
    _, _, _, leads = services
    lead = leads.create_lead(submitted_values={
        "lead_field__first_name": "Erika", "lead_field__last_name": "Example", "lead_field__company": company,
        "lead_field__street": "Main Street 12a", "lead_field__postal_code": "01234", "lead_field__city": "City",
        "lead_field__email": "private@example.test",
    })
    detail = leads.get_detail(lead_id=lead.id)
    assert query(detail.google_search_url) == (company.strip() or "Erika Example") + " Main Street 12a 01234 City"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


@pytest.mark.parametrize("template", ["customer_detail.html", "lead_detail.html"])
@pytest.mark.parametrize("can_manage", [False, True])
def test_detail_menu_search_is_read_only_and_opens_safe_new_tab(template, can_manage):
    template_dir = Path(__file__).resolve().parents[1] / "app/templates"
    source = (template_dir / template).read_text(encoding="utf-8")
    header = "{% call module_detail_header" + source.split("{% call module_detail_header", 1)[1].split("{% endcall %}", 1)[0] + "{% endcall %}"
    env = Environment(loader=FileSystemLoader(template_dir), autoescape=True)
    url = business_google_search_url('Example " & GmbH', "Main Street 12a", "01234", "City")
    detail = SimpleNamespace(
        name="Example", status="Neu", google_search_url=url, fields=(), subforms=(), editable_profile_fields=(),
        display_profile_fields=("name",), entry=SimpleNamespace(customer=SimpleNamespace(id=1, name="Example", zoho_id=None), account_status="Neu"),
    )
    rendered = env.from_string('{% from "partials/detail_header.html" import module_detail_header %}' + header).render(
        detail=detail, can_manage_customer_fields=can_manage, can_manage_communications=can_manage,
        can_send_website_profile=can_manage, can_delete_lead=can_manage, can_edit_lead=can_manage,
    )
    parser = Links()
    parser.feed(rendered)
    links = [link for link in parser.links if link.get("href") == url]
    assert len(links) == 1
    assert links[0]["target"] == "_blank"
    assert set(links[0]["rel"].split()) == {"noopener", "noreferrer"}
    assert "Google Suche</a>" in rendered
    if template == "lead_detail.html":
        assert ("data-lead-delete-open" in rendered) == can_manage
