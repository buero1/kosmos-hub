import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json

import pytest

from app.api.routes import web
from app.models.customer import Customer
from app.services.customer_directory import CUSTOMER_FIELDS_LAYOUT_KEY, CustomerDirectoryService
from app.services.customer_profile import resolve_customer_fields
from app.services.module_layout_catalog import get_module_layout_definition
from app.services.module_layouts import ModuleLayoutService
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS
from app.services.zoho_crm import ZohoCrmService
from test_customer_edit_layout import Controls, render_grids
from test_customer_empty_fields import customer_fields
from test_hub_record_operations import env, request


@pytest.mark.parametrize("definition", [field for field in ZOHO_ACCOUNT_FIELDS if not field.subform_parent], ids=lambda field: field.key)
def test_catalog_identity_survives_a_changed_metadata_label(definition):
    profile = {"fields": {definition.label: "value"}, "field_metadata": {definition.key: {
        "label": "Old label for " + definition.key, "editable": True,
    }}}
    original = deepcopy(profile)
    fields = resolve_customer_fields(profile)
    assert len(fields) == 1
    field = fields[0]
    assert (field.key, field.label, field.value) == (definition.key, definition.label, "value")
    assert field.definition["editable"]
    assert field.definition["sensitive"] == definition.sensitive
    assert profile == original


@pytest.mark.parametrize("stored_label", ["Website", "Webseite", "website"])
@pytest.mark.parametrize("value", [None, "", "https://example.test"])
def test_website_uses_saved_layout_slot_in_both_modes(customer_fields, stored_label, value):
    db, service, customer, admin = customer_fields
    profile = service._profile_data(customer)
    profile["field_metadata"]["website"]["label"] = "Webseite"
    profile["fields"].pop("Website")
    profile["fields"][stored_label] = value
    customer.encrypted_profile_json = service.cipher.encrypt(json.dumps(profile))
    catalog = get_module_layout_definition(CUSTOMER_FIELDS_LAYOUT_KEY)
    keys = tuple(field.key for field in catalog.fields)
    order = ("phone", "website", "customer_name", ModuleLayoutService.SHOW_MORE_ITEM_KEY) + tuple(
        key for key in keys if key not in {"phone", "website", "customer_name"})
    ModuleLayoutService(db=db).configure(actor=admin, layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
        item_order_json=json.dumps(order), allowed_keys=keys)
    detail = service.get_detail(customer_id=customer.id, include_sensitive=True)
    expected, split = ModuleLayoutService(db=db).ordered_keys_with_show_more(
        layout_key=CUSTOMER_FIELDS_LAYOUT_KEY, default_keys=keys)
    assert tuple(field.key for field in detail.display_profile_fields) == expected
    assert detail.show_more_index == split == 3
    assert detail.summary_profile_fields[1].key == "website"
    assert detail.summary_profile_fields[1].editable
    assert detail.summary_profile_fields[1].label == "Website"
    controls = Controls(render_grids(detail))
    assert controls.groups == [list(expected[:split]), list(expected[split:])]
    assert controls.names.count("customer_field__website") == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_current_label_wins_over_stale_alias_even_when_explicitly_empty(reverse):
    values = [("Webseite", "https://stale.test"), ("Website", None)]
    fields = resolve_customer_fields({"fields": dict(reversed(values) if reverse else values),
        "field_metadata": {"website": {"label": "Webseite", "editable": True}}})
    assert len(fields) == 1 and fields[0].key == "website" and fields[0].value is None


def test_missing_value_keeps_declared_field_but_does_not_escalate_edit_rights():
    fields = resolve_customer_fields({"fields": {}, "field_metadata": {
        "website": {"label": "Webseite", "editable": False},
        "ws_updates": {"label": "WS-Updates", "subform_parent": True},
    }})
    assert len(fields) == 1 and fields[0].key == "website"
    assert fields[0].value is None and fields[0].definition["editable"] is False
    fields = resolve_customer_fields({"fields": {"Website": "https://example.test", "IBAN": "secret"}})
    assert not fields[0].definition.get("editable")
    assert fields[1].definition["sensitive"]


def test_all_renamed_fields_match_layout_and_preserve_sensitive_filter(customer_fields):
    _, service, customer, _ = customer_fields
    profile = service._profile_data(customer)
    for key, metadata in profile["field_metadata"].items():
        metadata["label"] = "Former " + key
        metadata["sensitive"] = False
    customer.encrypted_profile_json = service.cipher.encrypt(json.dumps(profile))
    catalog = get_module_layout_definition(CUSTOMER_FIELDS_LAYOUT_KEY)
    admin = service.get_detail(customer_id=customer.id, include_sensitive=True)
    assert {field.key for field in admin.display_profile_fields} == {field.key for field in catalog.fields}
    regular = service.get_detail(customer_id=customer.id)
    assert {"iban", "dialfire_api_token"}.isdisjoint(field.key for field in regular.display_profile_fields)


@pytest.mark.parametrize("path", ["ui", "agent"])
def test_renamed_website_remains_writable_via_shared_operation(env, path):
    profile = {"fields": {"Kunde-Name": "Example", "Website": "https://before.test"},
        "field_metadata": {"website": {"label": "Webseite", "api_name": "Website", "display_type": "URL", "editable": True}}}
    customer = Customer(name="Example", zoho_id="test-account", encrypted_profile_json=env.cipher.encrypt(json.dumps(profile)))
    env.db.add(customer)
    env.db.flush()
    directory = CustomerDirectoryService(db=env.db, cipher=env.cipher)
    writes = []

    def update(self, *, customer_id, submitted_values):
        stored = directory._profile_data(customer)
        changes = self._root_field_changes(stored["field_metadata"], stored, submitted_values)
        writes.append(changes)
        refreshed = self._build_profile({"id": "test-account", "Website": changes["Website"]}, {"website": "Website"},
            {"metadata": profile["field_metadata"]}, datetime.now(UTC))
        customer.encrypted_profile_json = env.cipher.encrypt(json.dumps(refreshed))
        env.db.flush()
        return customer

    env.monkeypatch.setattr(ZohoCrmService, "update_customer_fields", update)
    if path == "ui":
        response = asyncio.run(web.update_customer_fields(customer.id, request(env, {"customer_field__website": "https://after.test"}), env.db))
        assert response.status_code == 303
    else:
        env.service.execute("customers.update", {"customer_id": str(customer.id), "website": "https://after.test"})
    assert writes == [{"Website": "https://after.test"}]
    for _ in range(2):
        detail = directory.get_detail(customer_id=customer.id)
        website = next(field for field in detail.display_profile_fields if field.key == "website")
        assert website.value == "https://after.test" and website.editable
        assert website.label == "Website"
    assert directory._profile_data(customer)["field_metadata"]["website"]["label"] == "Website"


@pytest.mark.parametrize("label", ["Webseite", "Website", "website"])
def test_unchanged_website_does_not_trigger_a_write_for_any_persisted_label(customer_fields, label):
    db, directory, _, _ = customer_fields
    service = ZohoCrmService(db=db, cipher=directory.cipher, public_base_url="https://hub.example")
    metadata = {"website": {"label": "Webseite", "api_name": "Website", "editable": True}}
    assert service._root_field_changes(metadata, {"fields": {label: "https://example.test"}},
        {"customer_field__website": "https://example.test"}) == {}
