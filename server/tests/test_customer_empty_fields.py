import json
from html import unescape
from pathlib import Path
import re

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.services.customer_directory import CUSTOMER_FIELDS_LAYOUT_KEY, CustomerDirectoryService, CustomerProfileField
from app.services.module_layout_catalog import get_module_layout_definition
from app.services.module_layouts import ModuleLayoutService
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS


@pytest.fixture
def customer_fields():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        definitions = tuple(field for field in ZOHO_ACCOUNT_FIELDS if not field.subform_parent)
        values = {field.label: None for field in definitions}
        values.update({"Kunde-Name": "Example Customer", "Options an WP senden": False, "Dauer in Minuten": 0})
        customer = Customer(name="Example Customer", encrypted_profile_json=cipher.encrypt(json.dumps({
            "fields": values,
            "field_metadata": {field.key: {
                "label": field.label, "display_type": field.display_type,
                "editable": True, "sensitive": field.sensitive,
            } for field in definitions},
        })))
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add_all([customer, admin])
        db.flush()
        yield db, CustomerDirectoryService(db=db, cipher=cipher), customer, admin
    engine.dispose()


def test_empty_fields_keep_saved_order_and_show_more_position(customer_fields):
    db, service, customer, admin = customer_fields
    catalog = get_module_layout_definition(CUSTOMER_FIELDS_LAYOUT_KEY)
    keys = tuple(field.key for field in catalog.fields)
    first = ("website", "customer_name", "contract_start", "hub_postal_city")
    order = first + (ModuleLayoutService.SHOW_MORE_ITEM_KEY,) + tuple(key for key in keys if key not in first)
    ModuleLayoutService(db=db).configure(
        actor=admin, layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
        item_order_json=json.dumps(order), allowed_keys=keys,
    )

    detail = service.get_detail(customer_id=customer.id, include_sensitive=True)
    fields = {field.key: field for field in detail.display_profile_fields}
    assert tuple(fields) == tuple(key for key in order if key != ModuleLayoutService.SHOW_MORE_ITEM_KEY)
    assert tuple(field.key for field in detail.summary_profile_fields) == first
    assert len(detail.following_profile_fields) == len(keys) - len(first)
    assert fields["website"].value is None
    assert fields["contract_start"].value is None
    assert not fields["hub_postal_city"].value
    assert "billing_postal_code" not in fields and "billing_city" not in fields
    assert fields["send_options_to_wordpress"].value == "False"
    assert fields["duration_minutes"].value == "0"
    assert [field.key for field in detail.editable_profile_fields[:3]] == list(first[:3])
    assert all(field.form_value == "" for field in detail.editable_profile_fields if field.key in {"website", "contract_start"})
    tabs = {tab.key: tab for tab in detail.profile_field_tabs}
    assert "bank" in {field.key for field in tabs["bank-details"].fields}
    assert "dialfire_comment" in {field.key for field in tabs["dialfire"].fields}


def test_empty_sensitive_fields_stay_hidden_without_permission(customer_fields):
    _, service, customer, _ = customer_fields
    public = service.get_detail(customer_id=customer.id)
    admin = service.get_detail(customer_id=customer.id, include_sensitive=True)
    sensitive = {field.key for field in ZOHO_ACCOUNT_FIELDS if field.sensitive}
    assert sensitive <= {field.key for field in admin.display_profile_fields}
    for collection in (public.profile_fields, public.display_profile_fields, public.editable_profile_fields,
                       *(tab.fields for tab in public.profile_field_tabs)):
        assert sensitive.isdisjoint(field.key for field in collection)


@pytest.mark.parametrize("postal_code, city, expected", [
    (None, None, ""), ("", "", ""), ("12345", None, "12345"),
    (None, "Example City", "Example City"), ("12345", "Example City", "12345 Example City"),
])
def test_postal_city_is_kept_even_when_both_parts_are_empty(customer_fields, postal_code, city, expected):
    _, service, _, _ = customer_fields
    fields, _ = service._profile_field_display_layout((
        CustomerProfileField(label="Postal code", key="billing_postal_code", value=postal_code),
        CustomerProfileField(label="City", key="billing_city", value=city),
    ))
    assert len(fields) == 1
    assert fields[0].key == "hub_postal_city"
    assert fields[0].value == expected


@pytest.mark.parametrize("key, value, expected", [
    ("website", None, "\u2013"), ("work_domain_login", "", "\u2013"),
    ("hub_postal_city", "", "\u2013"), ("contract_start", None, "\u2013"),
    ("duration_minutes", "0", "0"), ("send_options_to_wordpress", "Nein", "Nein"),
    ("important_info", "<script>example</script>", "<script>example</script>"),
])
def test_customer_value_macro_renders_empty_placeholder_without_links(key, value, expected):
    root = Path(__file__).resolve().parents[1] / "app/templates"
    source = (root / "customer_detail.html").read_text(encoding="utf-8")
    macro = re.search(r"{% macro customer_profile_field_value\(field\) %}.*?{% endmacro %}", source, re.S).group()
    env = Environment(loader=FileSystemLoader(root), autoescape=True, undefined=StrictUndefined)
    template = env.from_string(macro + "{{ customer_profile_field_value(field) }}")
    rendered = template.render(field=CustomerProfileField(key=key, label=key, value=value)).strip()
    assert unescape(rendered) == expected
    assert "<a " not in rendered
    assert "<script>" not in rendered


def test_new_hub_customer_keeps_its_empty_optional_fields(customer_fields):
    _, service, _, _ = customer_fields
    customer = service.create_hub_customer(submitted_values={
        "customer_field__customer_name": "New Customer", "customer_field__account_status": "Neu",
    })
    detail = service.get_detail(customer_id=customer.id)
    fields = {field.key: field for field in detail.display_profile_fields}
    assert {"website", "phone", "industry", "billing_street", "hub_postal_city", "important_info"} <= fields.keys()
    assert fields["website"].value is None
    assert not fields["hub_postal_city"].value
