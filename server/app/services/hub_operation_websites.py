"""Shared, permission-scoped local website inventory; no remote execution."""
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload
from app.core.timezones import iso_berlin_time

from app.models.customer import Customer
from app.models.site import Site
from app.repositories.site_repository import SiteRepository
from app.services.customer_directory import CustomerDirectoryService
from app.services.fleet_inventory import FleetInventoryService
from app.services.hub_operations import (
    HubOperation, HubOperationError, HubOperationInputField as Field, HubOperationResult,
    HubQuery, register_operation, register_query,
)
from app.services.hub_record_access import identifier, require_actor


def website_sites(service, *, limit=None):
    user, access = require_actor(service, "websites", "view")
    statement = select(Site).options(selectinload(Site.customer), selectinload(Site.connections), selectinload(Site.capabilities))
    allowed = access.accessible_record_ids(user=user, module_key="websites")
    if allowed is not None:
        statement = statement.where(Site.id.in_(allowed))
    customers = access.accessible_record_ids(user=user, module_key="customers")
    if not access.can(user, "customers", "view"):
        customers = set()
    if customers is not None:
        statement = statement.where(or_(Site.customer_id.is_(None), Site.customer_id.in_(customers)))
    statement = statement.order_by(Site.domain.asc(), Site.id.asc())
    if limit is not None:
        statement = statement.limit(limit)
    return list(service.db.scalars(statement))


def website_site(service, site_id, *, action="view"):
    user, access = require_actor(service, "websites", action)
    if not isinstance(site_id, int) or site_id < 1 or site_id >= 2**63:
        raise HubOperationError("Die Website-ID ist ungueltig.")
    if not access.can_access_record(user=user, module_key="websites", record_id=site_id, action=action):
        raise HubOperationError("Die Website ist nicht verfuegbar.")
    site = SiteRepository(service.db).get_site(site_id)
    if site is None or (site.customer_id is not None and not access.can_access_record(
        user=user, module_key="customers", record_id=site.customer_id,
    )):
        raise HubOperationError("Die Website ist nicht verfuegbar.")
    return site


def require_website_selection_access(service, site_ids):
    """Check read access in bulk without loading inventory or historical payloads."""
    user, access = require_actor(service, "websites", "view")
    targets = set(site_ids)
    if any(type(site_id) is not int or site_id < 1 or site_id >= 2**63 for site_id in targets):
        raise HubOperationError("Die Website-ID ist ungueltig.")
    if not targets:
        return
    allowed_sites = access.accessible_record_ids(user=user, module_key="websites")
    allowed_customers = access.accessible_record_ids(user=user, module_key="customers")
    if not access.can(user, "customers", "view"):
        allowed_customers = set()
    statement = select(Site.id).where(Site.id.in_(targets))
    if allowed_sites is not None:
        statement = statement.where(Site.id.in_(allowed_sites))
    if allowed_customers is not None:
        statement = statement.where(or_(Site.customer_id.is_(None), Site.customer_id.in_(allowed_customers)))
    if set(service.db.scalars(statement)) != targets:
        raise HubOperationError("Die Website ist nicht verfuegbar.")


def website_items(service, *, limit=None):
    return FleetInventoryService(db=service.db, cipher=service.cipher).items_for_sites(website_sites(service, limit=limit))


def website_inventory(service, site_id):
    site = website_site(service, site_id)
    return FleetInventoryService(db=service.db, cipher=service.cipher).items_for_sites([site])[0]


def metadata(site):
    return {"site_id": str(site.id), "domain": site.domain, "home_url": site.home_url,
        "status": site.status, "wordpress_version": site.wordpress_version, "php_version": site.php_version,
        "bridge_version": site.bridge_version, "customer_id": str(site.customer_id or ""),
        "customer_name": site.customer.name if site.customer is not None else "", "href": f"/sites/{site.id}"}


def page(items, values):
    raw = values.get("offset") or "0"
    if not raw.isdecimal() or len(raw) > 7:
        raise HubOperationError("Ungueltiger Lese-Offset.")
    start = int(raw)
    return {"items": items[start:start + 25], "total": len(items),
        "next_offset": str(start + 25) if len(items) > start + 25 else ""}


def search(service, values):
    inventory = FleetInventoryService(db=service.db, cipher=service.cipher)
    items = inventory.filter_items(website_items(service), **{key: value for key, value in values.items() if key != "offset"})
    return page([metadata(item.site) for item in items], values)


def read(service, values):
    item = website_inventory(service, identifier(values["site_id"]))
    return {**metadata(item.site), "inventory_captured_at": iso_berlin_time(item.snapshot.captured_at) if item.snapshot else None,
        "updates_captured_at": iso_berlin_time(item.update_snapshot.captured_at) if item.update_snapshot else None,
        "plugin_count": item.plugin_count, "update_count": item.update_count,
        "data_source": "Gespeicherter Hub-Bestand, keine Live-Abfrage der Website."}


def inventory_details(service, values):
    item = website_inventory(service, identifier(values["site_id"]))
    section = values.get("section", "plugins") or "plugins"
    if section == "plugins":
        records = item.plugins
        keys = ("name", "plugin_file", "version", "is_active", "active")
    else:
        records = getattr(item, section)
        keys = ("name", "plugin_file", "stylesheet", "current_version", "new_version", "version", "is_active")
    # Never expose raw plugin payloads or package/license URLs to the model.
    items = [{key: value[:500] if isinstance(value, str) else value
              for key in keys if isinstance((value := record.get(key)), (str, int, float, bool))}
             for record in records]
    return {**page(items, values), "site_id": str(item.site.id), "section": section,
        "captured_at": (iso_berlin_time(item.snapshot.captured_at) if item.snapshot else None) if section == "plugins"
        else (iso_berlin_time(item.update_snapshot.captured_at) if item.update_snapshot else None)}


SITE_ID_FIELD = Field("site_id", "Website-ID", required=True, max_length=18)
LINK_FIELDS = (SITE_ID_FIELD, Field("customer_id", "Kunden-ID", required=True, context_type="customer", max_length=18))


def link_customer(service, values):
    if set(values) - {field.name for field in LINK_FIELDS}:
        raise HubOperationError("Unbekannte Eingaben zur Website-Zuordnung.")
    for field in LINK_FIELDS:
        value = values.get(field.name, "")
        if not isinstance(value, str) or not value.strip() or len(value) > field.max_length:
            raise HubOperationError(f"{field.label} fehlt oder ist ungueltig.")
    site_id, customer_id = identifier(values["site_id"]), identifier(values["customer_id"])
    user, access = require_actor(service, "customers", "edit")
    if not access.can_access_record(user=user, module_key="customers", record_id=customer_id, action="edit"):
        raise HubOperationError("Der Kunde ist nicht verfuegbar.")
    service.db.scalar(select(Customer).where(Customer.id == customer_id).with_for_update().execution_options(populate_existing=True))
    service.db.scalar(select(Site).where(Site.id == site_id).with_for_update().execution_options(populate_existing=True))
    website_site(service, site_id, action="edit")
    try:
        customer, site = CustomerDirectoryService(db=service.db, cipher=service.cipher).link_exact_match(customer_id=customer_id, site_id=site_id)
    except ValueError as exc:
        raise HubOperationError(str(exc)) from exc
    service.db.expire(site, ["customer"])
    return HubOperationResult("Website oeffnen", f"/sites/{site.id}", site.id,
        outputs={"site_id": str(site.id), "customer_id": str(customer.id), "customer_name": customer.name, "domain": site.domain})


SEARCH_FIELDS = (
    Field("query", "Domain oder Website-URL"), Field("plugin", "Installiertes Plugin"),
    Field("status", "Website-Status"), Field("customer_status", "Kundenstatus oder unlinked"),
    Field("inventory_state", "Inventar", options=tuple((key, key) for key in ("all", "present", "missing"))),
    Field("updates_state", "Updates", options=tuple((key, key) for key in ("all", "available", "wordpress", "plugins", "themes", "none", "missing"))),
    Field("update_plugin", "Plugin mit Update"), Field("wordpress_version", "WordPress-Version"),
    Field("bridge_version", "Bridge-Version"), Field("offset", "Seitenbeginn"),
)
register_query(HubQuery("websites.list", "Websites mit denselben Inventarfiltern wie die Website-Liste suchen. Nur zugreifbare Websites/Kunden; je 25 Treffer. Kein Netzwerkzugriff.", SEARCH_FIELDS, search))
register_query(HubQuery("websites.read", "Website, Kundenzuordnung und Zeitpunkt des gespeicherten Inventars lesen. Keine Zugangsdaten und keine Live-Abfrage.", (SITE_ID_FIELD,), read))
register_query(HubQuery("websites.inventory", "Gespeicherte Plugins und verfuegbare Updates lesen, je 25 Eintraege. Inventar ist keine Erlaubnis zur Fern-Ausfuehrung.",
    (SITE_ID_FIELD, Field("section", "Inventarbereich", options=tuple((key, key) for key in ("plugins", "core_updates", "plugin_updates", "theme_updates"))), Field("offset", "Seitenbeginn")), inventory_details))
register_operation(HubOperation(key="websites.link_customer", module="websites", label="Website mit Kunde verknuepfen",
    description="Vorhandene Website mit einem Kunden verknuepfen, wie in der Domain-Pruefliste. Nur eindeutiger exakter Domain-Treffer, keine bestehende Zuordnung ueberschreiben.",
    input_guide="IDs aus den gemeinsamen Lesezugriffen verwenden. Bearbeitungsrecht fuer Kunde und Website erforderlich. Keine externe CRM-Verbindung.",
    input_fields=lambda: LINK_FIELDS, preview_fields=tuple((field.name, field.label) for field in LINK_FIELDS), execute=link_customer,
    result_fields=(("site_id", "Website-ID"), ("customer_id", "Kunden-ID"), ("customer_name", "Kundenname"), ("domain", "Domain"))))
