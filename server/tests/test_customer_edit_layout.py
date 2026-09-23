from html.parser import HTMLParser
import json
from pathlib import Path
import re
from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined
import pytest

from app.services.customer_directory import CUSTOMER_FIELDS_LAYOUT_KEY, CustomerProfileField
from app.services.module_layouts import ModuleLayoutService
from test_customer_empty_fields import customer_fields


TEMPLATE = Path(__file__).resolve().parents[1] / "app/templates/customer_detail.html"


def render_grids(detail):
    source = TEMPLATE.read_text(encoding="utf-8")
    names = ("customer_profile_field_value", "customer_field_control", "customer_readonly_field_control", "customer_profile_edit_grid")
    macros = "\n".join(re.search(r"{% macro " + name + r"\(.*?{% endmacro %}", source, re.S).group() for name in names)
    env = Environment(autoescape=True, undefined=StrictUndefined)
    return env.from_string(macros + "{{ customer_profile_edit_grid(detail.summary_profile_fields) }}"
                           "{{ customer_profile_edit_grid(detail.following_profile_fields, following=true) }}").render(detail=detail)


class Controls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.names = []
        self.groups = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "customer-profile-edit-grid" in attrs.get("class", "").split():
            self.groups.append([])
        if "data-customer-field-key" in attrs:
            self.groups[-1].append(attrs["data-customer-field-key"])
        if tag in {"input", "select", "textarea"}:
            self.names.append(attrs["name"])


@pytest.mark.parametrize("split", [1, 13, 14])
def test_editor_preserves_all_reading_slots_and_divider(customer_fields, split):
    db, service, customer, admin = customer_fields
    profile = service._profile_data(customer)
    for key in ("work_domain", "record_id", "annual_cycle"):
        profile["field_metadata"][key]["editable"] = False
    customer.encrypted_profile_json = service.cipher.encrypt(json.dumps(profile))
    db.flush()
    keys = tuple(field.key for field in service.get_detail(customer_id=customer.id, include_sensitive=True).display_profile_fields)
    keys = ("work_domain", "hub_postal_city") + tuple(key for key in reversed(keys) if key not in {"work_domain", "hub_postal_city"})
    ModuleLayoutService(db=db).configure(actor=admin, layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
        item_order_json=json.dumps(keys[:split] + (ModuleLayoutService.SHOW_MORE_ITEM_KEY,) + keys[split:]), allowed_keys=keys)
    detail = service.get_detail(customer_id=customer.id, include_sensitive=True)
    rendered = render_grids(detail)
    controls = Controls(rendered)
    assert controls.groups == [list(keys[:split]), list(keys[split:])]
    expected_names = {"customer_field__" + field.key for field in detail.editable_profile_fields}
    assert set(controls.names) == expected_names
    assert len(controls.names) == len(expected_names)
    assert "customer_field__work_domain" not in controls.names
    assert "customer_field__hub_postal_city" not in controls.names
    assert "customer_field__billing_postal_code" in controls.names
    assert "customer_field__billing_city" in controls.names
    assert "(nur Anzeige)" in rendered


def test_editor_does_not_reintroduce_sensitive_fields(customer_fields):
    _, service, customer, _ = customer_fields
    detail = service.get_detail(customer_id=customer.id)
    rendered = render_grids(detail)
    controls = Controls(rendered)
    assert "customer_field__iban" not in controls.names
    assert "customer_field__dialfire_api_token" not in controls.names
    assert "iban" not in controls.groups[0] + controls.groups[1]


@pytest.mark.parametrize("editable", [True, False])
def test_postal_city_controls_share_one_slot_with_unchanged_names(editable):
    fields = (
        CustomerProfileField(key="billing_postal_code", label="Postal code", value="01234", form_value="01234", editable=editable),
        CustomerProfileField(key="billing_city", label="City", value="Example City", form_value="Example City", editable=editable),
    )
    detail = SimpleNamespace(profile_fields=fields,
        summary_profile_fields=(CustomerProfileField(key="hub_postal_city", label="PLZ Ort", value="01234 Example City"),),
        following_profile_fields=(), wordpress_admin_site=None)
    rendered = render_grids(detail)
    controls = Controls(rendered)
    assert controls.groups == [["hub_postal_city"], []]
    assert set(controls.names) == ({"customer_field__billing_postal_code", "customer_field__billing_city"} if editable else set())
    assert "01234" in rendered and "Example City" in rendered
    assert "<legend>PLZ Ort</legend>" in rendered


def test_readonly_slot_does_not_enable_editing_and_escapes_content():
    field = CustomerProfileField(key="work_domain", label="Work domain", value="<script>unsafe</script>", editable=False)
    detail = SimpleNamespace(profile_fields=(field,), summary_profile_fields=(field,), following_profile_fields=(), wordpress_admin_site=None)
    rendered = render_grids(detail)
    assert Controls(rendered).names == []
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in rendered
    assert "<script>" not in rendered


def test_customer_edit_template_uses_the_same_sections_as_reading():
    source = TEMPLATE.read_text(encoding="utf-8")
    form = source.split('<form id="customer-fields-form"', 1)[1].split('</form>', 1)[0]
    assert "customer_profile_edit_grid(detail.summary_profile_fields)" in form
    assert "customer_profile_edit_grid(detail.following_profile_fields, following=true)" in form
    assert "for field in detail.editable_profile_fields" not in form
