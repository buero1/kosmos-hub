from datetime import UTC, datetime
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.models.site import Site
from app.models.site_snapshot import SiteSnapshot
from app.models.site_update_snapshot import SiteUpdateSnapshot
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_operations import HubOperationError, HubOperationService
from app.services.hub_operation_websites import website_sites, website_inventory


@pytest.fixture
def hub():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(HubUser(username="admin", password_hash="hash", role="admin"))
        db.commit()
        yield HubOperationService(db=db, cipher=None, actor="admin")
    engine.dispose()


def add_site(hub, index=1):
    site = Site(uuid=f"site-{index}", domain=f"site{index:03}.example", home_url=f"https://site{index:03}.example", site_url=f"https://site{index:03}.example", wordpress_version="6.8.2", status="verified")
    hub.db.add(site)
    hub.db.flush()
    return site


def test_ui_api_and_agent_share_website_selection_and_inventory(hub):
    from app.api.routes.sites import list_sites, get_site

    site = add_site(hub)
    request = Request({"type": "http", "method": "GET", "path": "/api/v1/sites", "headers": []})
    request.state.hub_user = hub.db.query(HubUser).filter_by(username="admin").one()
    assert list_sites(request=request, db=hub.db).items[0].id == site.id
    assert get_site(site_id=site.id, request=request, db=hub.db).domain == site.domain
    assert hub.query("websites.list", {"query": site.domain})["items"][0]["site_id"] == str(site.id)
    assert website_inventory(hub, site.id).site == site
    assert hub.query("websites.read", {"site_id": str(site.id)})["inventory_captured_at"] is None


def test_inventory_is_local_paginated_and_excludes_secrets(hub):
    site = add_site(hub)
    hub.db.add(SiteSnapshot(site_id=site.id, captured_at=datetime.now(UTC),
        plugins_json=[{"name": f"Plugin{i}", "plugin_file": f"plugin{i}/p.php", "version": "1.0", "is_active": True, "license_key": "TOPSECRET"} for i in range(26)],
        themes_json=[], environment_json={"auth_token": "TOPSECRET"}))
    hub.db.add(SiteUpdateSnapshot(site_id=site.id, captured_at=datetime.now(UTC), core_updates_json=[],
        plugin_updates_json=[{"name": "Plugin0", "new_version": "2.0", "package": "https://example/?token=TOPSECRET"}], theme_updates_json=[], summary_json={}))
    hub.db.flush()
    first = hub.query("websites.inventory", {"site_id": str(site.id), "section": "plugins"})
    second = hub.query("websites.inventory", {"site_id": str(site.id), "section": "plugins", "offset": first["next_offset"]})
    assert len(first["items"]) == 25 and len(second["items"]) == 1
    updates = hub.query("websites.inventory", {"site_id": str(site.id), "section": "plugin_updates"})
    assert updates["items"][0]["new_version"] == "2.0"
    assert "TOPSECRET" not in json.dumps([first, second, updates])
    assert hub.query("websites.list", {"plugin": "Plugin0", "updates_state": "plugins"})["total"] == 1
    assert hub.query("websites.list", {"inventory_state": "missing"})["total"] == 0


def test_site_and_related_customer_scopes_apply_to_all_readers(hub):
    access = HubAccessControlService(db=hub.db)
    access.save_role(role_key="website-reader", name="Websites", description="test", permissions={
        "websites": {"view": True, "edit": True, "scope": "all"},
        "customers": {"view": True, "edit": True, "scope": "none"},
    })
    user = HubUser(username="reader", role="website-reader", password_hash="hash")
    customer = Customer(name="Hidden customer")
    hub.db.add_all([user, customer])
    visible, hidden = add_site(hub), add_site(hub, 2)
    hidden.customer_id = customer.id
    hub.actor = user.username
    assert [site.id for site in website_sites(hub)] == [visible.id]
    assert hub.query("websites.list", {})["total"] == 1
    with pytest.raises(HubOperationError):
        hub.query("websites.read", {"site_id": str(hidden.id)})
    with pytest.raises(HubOperationError):
        hub.execute("websites.link_customer", {"site_id": str(visible.id), "customer_id": str(customer.id)})
    access.add_grant(module_key="customers", record_id=customer.id, user_id=user.id, team_id=None, can_edit=False)
    assert hub.query("websites.list", {})["total"] == 2
    user.is_active = False
    hub.db.flush()
    with pytest.raises(HubOperationError):
        hub.query("websites.list", {})


def test_link_uses_exact_match_rule_without_committing(hub):
    site = add_site(hub)
    customer = Customer(name="Kunde", website_domain=site.domain)
    hub.db.add(customer)
    hub.db.commit()
    assert hub.query("websites.read", {"site_id": str(site.id)})["customer_name"] == ""
    result = hub.execute("websites.link_customer", {"site_id": str(site.id), "customer_id": str(customer.id)})
    assert result.outputs["customer_id"] == str(customer.id)
    assert site.customer_id == customer.id
    assert hub.query("websites.read", {"site_id": str(site.id)})["customer_name"] == "Kunde"
    with pytest.raises(HubOperationError, match="already linked"):
        hub.execute("websites.link_customer", {"site_id": str(site.id), "customer_id": str(customer.id)})
    hub.db.rollback()
    assert site.customer_id is None
    customer.website_domain = "another.example"
    hub.db.flush()
    with pytest.raises(HubOperationError, match="exact"):
        hub.execute("websites.link_customer", {"site_id": str(site.id), "customer_id": str(customer.id)})


def test_shared_selection_preserves_website_and_customer_access_across_pages(hub):
    access = HubAccessControlService(db=hub.db)
    access.save_role(role_key="selection-reader", name="Reader", description="Test", permissions={
        "websites": {"view": True, "scope": "all"}, "customers": {"view": True, "scope": "none"}})
    visible_customer, hidden_customer = Customer(name="Visible"), Customer(name="Hidden")
    reader = HubUser(username="selection-user", role="selection-reader", password_hash="hash")
    hub.db.add_all([visible_customer, hidden_customer, reader])
    hub.db.flush()
    access.add_grant(module_key="customers", record_id=visible_customer.id, user_id=reader.id, team_id=None, can_edit=False)
    expected = []
    for index in range(31):
        site = add_site(hub, index + 1)
        site.customer_id = hidden_customer.id if index == 30 else visible_customer.id
        site.wordpress_version = "0.1" if index == 30 else "6.2.12" if index in (0, 29) else "6.10.0"
        if index in (0, 29):
            expected.append(str(site.id))
    hub.db.flush()
    hub.actor = reader.username
    options = {"fields": ["site_id", "customer_name", "wordpress_version"],
        "extreme": {"field": "wordpress_version", "kind": "min", "type": "version"}}
    result = hub.query("websites.list", {}, selection=options)
    assert result["source_total"] == 30 and result["source_pages"] == 2
    assert [row["site_id"] for row in result["items"]] == expected
    assert "Hidden" not in json.dumps(result) and "0.1" not in json.dumps(result)
    reader.is_active = False
    hub.db.flush()
    with pytest.raises(HubOperationError):
        hub.query("websites.list", {}, selection=options)


def test_site_search_pages_in_stable_order(hub):
    for index in range(26):
        add_site(hub, index)
    first = hub.query("websites.list", {})
    second = hub.query("websites.list", {"offset": first["next_offset"]})
    assert first["total"] == 26
    assert len(first["items"]) == 25 and len(second["items"]) == 1
    assert not set(item["site_id"] for item in first["items"]) & set(item["site_id"] for item in second["items"])
    for args in ({"offset": "-1"}, {"updates_state": "arbitrary"}, {"unknown": "x"}):
        with pytest.raises(HubOperationError):
            hub.query("websites.list", args)
