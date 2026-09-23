"""Permission-scoped native readers shared by web pages and catalog adapters."""
from datetime import UTC, datetime, timedelta

from app.services.hub_operations import HubOperationError
from app.services.hub_record_access import require_actor
from app.services.hub_operation_websites import website_sites, website_items, website_site, require_website_selection_access
from app.services.fleet_inventory import FleetInventoryService
from app.services.fleet_refresh import FleetRefreshService
from app.services.site_users import SiteUserService
from app.services.site_inventory import SiteInventoryService
from app.services.site_selection import SELECTABLE_CUSTOMER_STATUSES
from app.services.maintenance_runs import MaintenanceRunService
from app.repositories.site_repository import SiteRepository
from app.services.wordpress_status import _fleet_refresh_status_payload, _direct_update_batch_status_payload, _complete_site_update_status_payload


def require_admin(service, action="view"):
    user, access = require_actor(service, "websites", action)
    if user.role != "admin":
        raise HubOperationError("Diese WordPress-Verwaltung ist nur fuer Hub-Administratoren verfuegbar.")
    return user


def dashboard_data(service):
    user, access = require_actor(service, "dashboard", "view")
    items = website_items(service) if access.can(user, "websites", "view") else []
    sites = [item.site for item in items]
    cutoff = datetime.now(UTC) - timedelta(days=2)
    summary = {"total_sites": len(sites), "pending_sites": sum(s.status == "pending" for s in sites),
        "verified_sites": sum(s.status == "verified" for s in sites),
        "unknown_sites": sum(s.last_seen_at is None or s.last_seen_at.replace(tzinfo=UTC) < cutoff for s in sites)}
    return {"summary": summary, "sites": sorted(sites, key=lambda s: s.id, reverse=True)[:10],
        "inventory_summary": FleetInventoryService(db=service.db, cipher=service.cipher).summarize(items)}


def update_workbench(service, **filters):
    inventory = FleetInventoryService(db=service.db, cipher=service.cipher)
    items = website_items(service)
    entries = inventory.build_update_workbench(items)
    filtered = inventory.filter_update_workbench(entries, **filters)
    return items, entries, filtered, inventory.summarize_update_workbench(filtered)


def user_entries(service):
    require_admin(service)
    allowed = {site.id for site in website_sites(service)}
    return [entry for entry in SiteUserService(db=service.db, cipher=service.cipher).list_workbench_entries() if entry.site.id in allowed]


def backup_workbench(service, site_ids=None):
    require_admin(service)
    options = [site for site in website_sites(service) if site.status == "verified" and site.customer is not None
        and site.customer.zoho_status in SELECTABLE_CUSTOMER_STATUSES]
    selected = [site for site in options if site_ids is None or site.id in site_ids]
    snapshots = SiteRepository(service.db).get_latest_backup_snapshots_by_site_ids([site.id for site in selected])
    return options, selected, snapshots


def stored_capabilities(service, site_id):
    website_site(service, site_id)
    return SiteInventoryService(db=service.db, cipher=service.cipher).list_site_capabilities(site_id)


def state_snapshot(service, site_id):
    website_site(service, site_id)
    return SiteInventoryService(db=service.db, cipher=service.cipher).get_latest_site_snapshot(site_id)


def maintenance_batch(service, batch_id, *, installation=False):
    maintenance = MaintenanceRunService(db=service.db, cipher=service.cipher)
    runs = maintenance.list_plugin_installation_batch(batch_id) if installation else maintenance.list_plugin_update_batch(batch_id)
    for run in runs:
        website_site(service, run.site_id)
    return runs


def batch_status(service, batch_id, *, installation=False):
    runs = maintenance_batch(service, batch_id, installation=installation)
    if not runs:
        raise HubOperationError("Dieser Wartungslauf ist nicht verfuegbar.")
    return _direct_update_batch_status_payload(batch_id, runs)


def complete_run(service, run_id):
    run = MaintenanceRunService(db=service.db, cipher=service.cipher).get_complete_site_update_run(run_id)
    if run is None:
        raise HubOperationError("Dieser Wartungslauf ist nicht verfuegbar.")
    website_site(service, run.site_id)
    return run


def complete_status(service, run_id):
    run = complete_run(service, run_id)
    children = MaintenanceRunService(db=service.db, cipher=service.cipher).complete_site_update_child_runs(run.id)
    for child in children:
        website_site(service, child.site_id)
    return _complete_site_update_status_payload(run, children)


def fleet_run(service, run_id):
    require_admin(service)
    run = FleetRefreshService(db=service.db).get_run(run_id)
    if run is None:
        raise HubOperationError("Dieser Prueflauf ist nicht verfuegbar.")
    targets = FleetRefreshService._target_site_ids(run.result_json)
    if targets:
        require_website_selection_access(service, targets)
    return run


def fleet_status(service, run_id):
    return _fleet_refresh_status_payload(fleet_run(service, run_id))


def fleet_history(service, *, modes=None):
    require_admin(service)
    return FleetRefreshService(db=service.db).list_recent_runs(limit=20, modes=modes)


def is_removable_empty_test_registration(site):
    return (site.domain.startswith("test-") and site.domain.endswith(".kosmos-medien.de")
        and site.customer_id is None and not site.snapshots and not site.update_snapshots
        and not site.backup_snapshots and not site.user_snapshots and not site.capabilities
        and not site.update_plan_items and not site.maintenance_runs)
