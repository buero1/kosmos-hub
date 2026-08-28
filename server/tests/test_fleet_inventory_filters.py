from types import SimpleNamespace

from app.services.fleet_inventory import FleetInventoryItem, FleetInventoryService


def _item(*, site_status: str, customer_status: str | None = None) -> FleetInventoryItem:
    customer = None if customer_status is None else SimpleNamespace(zoho_status=customer_status)
    site = SimpleNamespace(
        status=site_status,
        customer=customer,
        wordpress_version=None,
        bridge_version=None,
        domain="example.test",
        home_url="https://example.test",
        site_url="https://example.test",
    )
    return FleetInventoryItem(site=site, snapshot=None, update_snapshot=None, plugins=())


def test_site_inventory_can_filter_by_linked_customer_status():
    service = FleetInventoryService(db=None, cipher=None)
    items = [_item(site_status="verified", customer_status="Aktuell"), _item(site_status="pending")]

    current_customers = service.filter_items(items, customer_status="Aktuell")
    unlinked_sites = service.filter_items(items, customer_status="unlinked")

    assert current_customers == [items[0]]
    assert unlinked_sites == [items[1]]


def test_update_workbench_distinguishes_all_sites_from_an_explicit_empty_selection():
    service = FleetInventoryService(db=None, cipher=None)
    entry = SimpleNamespace(site=SimpleNamespace(id=7))

    assert service.filter_update_workbench([entry], site_ids=None) == [entry]
    assert service.filter_update_workbench([entry], site_ids=set()) == []
