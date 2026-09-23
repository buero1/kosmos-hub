"""Local, access-checked evidence for preparing remote WordPress actions."""
from dataclasses import asdict
from app.core.timezones import iso_berlin_time

from app.services.fleet_inventory import FleetInventoryService
from app.services.hub_operation_websites import website_inventory, page, website_site
from app.services.hub_operations import HubOperationError
from app.services.hub_record_access import identifier, require_actor
from app.services.maintenance_runs import MaintenanceRunService
from app.services.plugin_installation_packages import PluginInstallationPackageService
from app.services.site_backups import SiteBackupService
from app.services.site_updates import SiteUpdateService
from app.services.site_users import SiteUserService
from app.services.user_deletion_batches import UserDeletionBatchService


def backup_snapshot(service, site_id):
    website_site(service, site_id)
    return SiteBackupService(db=service.db, cipher=service.cipher).get_latest_site_backup_snapshot(site_id)


def update_snapshot(service, site_id):
    website_site(service, site_id)
    return SiteUpdateService(db=service.db, cipher=service.cipher).get_latest_site_update_snapshot(site_id)


def read_update_snapshot(service, values):
    snapshot = update_snapshot(service, identifier(values["site_id"]))
    if snapshot is None:
        return {"captured_at": None, "items": [], "total": 0}
    items = [{"kind": kind, **{key: item.get(key) for key in
        ("name", "slug", "plugin", "theme", "current_version", "new_version", "version")}}
        for kind, entries in (("wordpress", snapshot.core_updates_json), ("plugin", snapshot.plugin_updates_json), ("theme", snapshot.theme_updates_json))
        for item in entries if isinstance(item, dict)]
    return {**page(items, values), "captured_at": iso_berlin_time(snapshot.captured_at)}


def users_inventory(service, site_id):
    user, _access = require_actor(service, "websites", "view")
    if user.role != "admin":
        raise HubOperationError("WordPress-Benutzer koennen nur Administratoren einsehen.")
    website_site(service, site_id)
    return SiteUserService(db=service.db, cipher=service.cipher).get_latest_inventory(site_id)


def read_users(service, values):
    site_id = identifier(values["site_id"])
    inventory = users_inventory(service, site_id)
    users = [] if inventory is None else inventory.users
    return {**page([{"selected_key": f"{site_id}:{user['id']}",
        **{key: user.get(key) for key in ("id", "username", "display_name", "email", "roles")}} for user in users], values),
        "available": bool(inventory and inventory.snapshot.available),
        "captured_at": iso_berlin_time(inventory.snapshot.captured_at) if inventory else None}


def read_backups(service, values):
    snapshot = backup_snapshot(service, identifier(values["site_id"]))
    backups = (snapshot.summary_json or {}).get("backups", []) if snapshot else []
    items = [{key: item.get(key) for key in ("backup_nonce", "backup_timestamp", "backup_at", "complete", "retention_protected", "components")} for item in backups]
    for item in items:
        item["selection"] = f"{item['backup_nonce']}:{item['backup_timestamp']}" if item.get("backup_nonce") and item.get("backup_timestamp") else ""
    return {**page(items, values), "captured_at": iso_berlin_time(snapshot.captured_at) if snapshot else None,
        "selection_format": "backup_nonce:backup_timestamp"}


def update_options(service, values):
    item = website_inventory(service, identifier(values["site_id"]))
    entries = FleetInventoryService(db=service.db, cipher=service.cipher).build_update_workbench([item])
    return page([{"selected_key": entry.plan_key, "kind": entry.kind, "name": entry.name,
        "current_version": entry.current_version, "target_version": entry.target_version,
        "selectable": entry.direct_update_selectable} for entry in entries], values)


def read_runs(service, values):
    site_id = identifier(values["site_id"])
    website_site(service, site_id)
    runs = MaintenanceRunService(db=service.db, cipher=service.cipher).list_site_runs(site_id)
    return page([{"run_id": str(run.id), "kind": run.kind, "status": run.status,
        "batch_id": (run.result_json or {}).get("batch_id", ""), "href": f"/sites/{site_id}"} for run in runs], values)


def plugin_catalog(service, values):
    user, _access = require_actor(service, "websites", "view")
    if user.role != "admin":
        raise HubOperationError("Der Plugin-Katalog ist nur fuer Administratoren verfuegbar.")
    raw_page = values.get("page") or "1"
    if not raw_page.isdecimal() or not 1 <= int(raw_page) <= 100:
        raise HubOperationError("Ungueltige Katalogseite.")
    return PluginInstallationPackageService(db=service.db).search_wordpress_org_plugins(
        search=values.get("search", ""), browse=values.get("browse") or "popular", page=int(raw_page))


def read_plugin_catalog(service, values):
    catalog = plugin_catalog(service, values)
    return {"items": [asdict(item) for item in catalog.items], "page": catalog.page, "pages": catalog.pages, "total": catalog.total}


def deletion_batch(service, batch_id):
    user, _access = require_actor(service, "websites", "view")
    if user.role != "admin":
        raise HubOperationError("Nur Administratoren duerfen Benutzerloeschungen einsehen.")
    batch = UserDeletionBatchService(db=service.db, cipher=service.cipher).get_batch(batch_id)
    if batch is None:
        raise HubOperationError("Dieser Loeschlauf ist nicht verfuegbar.")
    for item in batch.items:
        website_site(service, item.site_id)
    return batch


def read_deletion_batch(service, values):
    batch = deletion_batch(service, identifier(values["batch_id"]))
    rows = UserDeletionBatchService(db=service.db, cipher=service.cipher).preparation_rows(batch)
    items = [{"item_id": str(row["item"].id), "site_id": str(row["item"].site_id),
        "target_username": row["item"].target_username, "status": row["item"].status,
        "replacement_user_id": str(row["item"].replacement_user_id or ""),
        "candidates": [{"user_id": str(entry.user["id"]), "username": entry.user["username"]} for entry in row["candidates"]]}
        for row in rows]
    return {**page(items, values), "batch_id": str(batch.id), "status": batch.status, "href": f"/users?deletion_batch_id={batch.id}"}
