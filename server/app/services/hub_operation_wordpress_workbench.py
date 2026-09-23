"""Shared workbench reads and transactional fleet controls, without remote commits."""
import json
from functools import partial
from urllib.parse import urlencode

from sqlalchemy import select
from app.core.timezones import iso_berlin_time

from app.models.site import Site
from app.services.audit import write_audit_log
from app.services.hub_operations import HubOperation, HubOperationResult, HubOperationError, HubOperationInputField as Field, HubQuery, register_operation, register_query
from app.services.hub_record_access import identifier
from app.services.hub_operation_websites import metadata, page, website_sites, website_site
from app.services.wordpress_workbench import (require_admin, dashboard_data, update_workbench, user_entries, backup_workbench,
    stored_capabilities, state_snapshot, batch_status, complete_status, fleet_status, fleet_run, fleet_history, is_removable_empty_test_registration)
from app.services.fleet_refresh import FleetRefreshService
from app.services.site_users import SiteUserService


def selection(value):
    try:
        ids = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise HubOperationError("Website-Auswahl muss eine JSON-Liste sein.") from exc
    if not isinstance(ids, list) or len(ids) > 1000 or any(type(i) is not int or not 0 < i < 2**63 for i in ids):
        raise HubOperationError("Ungueltige Website-Auswahl.")
    return set(ids)


def selected(values):
    return selection(values["site_ids"]) if values.get("site_ids") else None


def start_fleet(service, values):
    user = require_admin(service, "edit")
    mode = values.get("mode", "")
    if mode not in FleetRefreshService.manual_modes():
        raise HubOperationError("Ungueltige Pruefart.")
    ids = selection(values.get("site_ids", ""))
    if not ids:
        raise HubOperationError("Mindestens eine konkrete Website auswaehlen.")
    for site_id in ids:
        website_site(service, site_id, action="edit")
    run, created = FleetRefreshService(db=service.db).create_run(actor=user, mode=mode, site_ids=ids)
    if created:
        run.result_json = {**run.result_json, "shared_contract": True}
    else:
        fleet_run(service, run.id)
    return HubOperationResult("Prueflauf ansehen", f"/updates?view=refresh-protocol&active_refresh_run_id={run.id}", run.id,
        outputs={"run_id": str(run.id), "created": "true" if created else "false", "mode": run.mode, "status": run.status})


def cancel_fleet(service, values):
    user = require_admin(service, "edit")
    run = fleet_run(service, identifier(values.get("run_id", "")))
    for site_id in FleetRefreshService._target_site_ids(run.result_json) or ():
        website_site(service, site_id, action="edit")
    run, cancelled = FleetRefreshService(db=service.db).cancel_run(actor=user, run_id=run.id)
    return HubOperationResult("Prueflauf ansehen", f"/updates?active_refresh_run_id={run.id}", run.id,
        outputs={"run_id": str(run.id), "cancelled": "true" if cancelled else "false", "status": run.status})


def remove_registration(service, values):
    site_id = identifier(values.get("site_id", ""))
    site = website_site(service, site_id, action="delete")
    service.db.scalar(select(Site).where(Site.id == site_id).with_for_update().execution_options(populate_existing=True))
    if not is_removable_empty_test_registration(site):
        raise HubOperationError("Nur leere, unverknuepfte Testregistrierungen ohne Wartungsverlauf koennen entfernt werden.")
    domain = site.domain
    service.db.delete(site)
    write_audit_log(service.db, site=None, actor=service.actor, source="hub-web", action="remove-empty-test-registration", result="ok", detail=f"Removed the empty test registration for {domain}.")
    service.db.flush()
    return HubOperationResult("Websites", f"/sites?{urlencode({'removed': domain})}", site_id)


def read_dashboard(service, values):
    data = dashboard_data(service)
    return {**data, "sites": [metadata(site) for site in data["sites"]]}


def read_fleet_history(service, values):
    return page([{"run_id": str(run.id), "mode": run.mode, "status": run.status,
        "created_at": iso_berlin_time(run.created_at), "href": f"/updates?view=refresh-protocol&refresh_run_id={run.id}"}
        for run in fleet_history(service)], values)


def read_updates(service, values):
    _items, _entries, entries, summary = update_workbench(service, site_ids=selected(values),
        **{key: value for key, value in values.items() if key not in {"offset", "site_ids"}})
    return {**page([{"selected_key": row.plan_key, "site_id": str(row.site.id), "domain": row.site.domain,
        "kind": row.kind, "name": row.name, "current_version": row.current_version, "target_version": row.target_version,
        "selectable": row.direct_update_selectable} for row in entries], values), "summary": summary}


def read_users(service, values):
    entries = SiteUserService.filter_workbench_entries(user_entries(service), site_ids=selected(values),
        **{key: value for key, value in values.items() if key not in {"offset", "site_ids"}})
    return page([{"selected_key": row.key, "site_id": str(row.site.id), "domain": row.site.domain,
        **{key: row.user.get(key) for key in ("id", "username", "display_name", "email", "roles")}} for row in entries], values)


def read_backups(service, values):
    _options, sites, snapshots = backup_workbench(service, selected(values))
    return page([{"site_id": str(site.id), "domain": site.domain, "available": bool(snapshots.get(site.id) and snapshots[site.id].backup_available),
        "complete": bool(snapshots.get(site.id) and snapshots[site.id].backup_complete),
        "captured_at": iso_berlin_time(snapshots[site.id].captured_at) if snapshots.get(site.id) else None} for site in sites], values)


def capabilities(service, values):
    return page([{"name": row.ability_name, "read_only": row.read_only, "destructive": row.destructive,
        "captured_at": iso_berlin_time(row.last_discovered_at)} for row in stored_capabilities(service, identifier(values["site_id"]))], values)


def state(service, values):
    snapshot = state_snapshot(service, identifier(values["site_id"]))
    if snapshot is None:
        return {"captured_at": None, "items": [], "total": 0}
    return {**page([{key: row.get(key) for key in ("name", "plugin_file", "version", "is_active")} for row in snapshot.plugins_json], values),
        "captured_at": iso_berlin_time(snapshot.captured_at), "wordpress_version": snapshot.wordpress_version, "php_version": snapshot.php_version}


def status_query(service, values, *, reader, id_field):
    record_id = values[id_field] if id_field == "batch_id" else identifier(values[id_field])
    data = reader(service, record_id)
    # UI keeps the full payload; the agent has explicit paging for each long list.
    pages = {}
    def bounded(node, prefix=""):
        if isinstance(node, dict):
            return {key: bounded(value, f"{prefix}.{key}".strip(".")) for key, value in node.items()}
        if isinstance(node, list):
            sliced = page(node, values)
            pages[prefix] = {key: sliced[key] for key in ("total", "next_offset")}
            return sliced["items"]
        return node
    result = bounded(data)
    result["pagination"] = pages
    return result


SITES = Field("site_ids", "Konkrete Website-IDs als JSON-Liste; bei Lesezugriff ohne Angabe alle sichtbaren", max_length=40000, encoding="JSON array")
OFFSET = Field("offset", "Seitenbeginn")
for key, reader, fields, description in (
    ("websites.dashboard", read_dashboard, (), "Dashboard-Zaehler nur fuer sichtbare Websites."),
    ("wordpress.fleet.list", read_fleet_history, (OFFSET,), "Die letzten 20 Inventar-Prueflaeufe wie in der Maske lesen. Nur Admin."),
    ("wordpress.workbench.updates", read_updates, (SITES, OFFSET, *(Field(k, k) for k in ("query", "kind", "activity", "diagnosis", "plugin_identifier"))), "Gespeicherte Update-Uebersicht mit denselben Filtern wie die Maske."),
    ("wordpress.workbench.users", read_users, (SITES, OFFSET, *(Field(k, k) for k in ("query", "role", "customer_status"))), "Gespeicherte Benutzeruebersicht wie die Maske. Nur Admin."),
    ("wordpress.workbench.backups", read_backups, (SITES, OFFSET), "Backup-Uebersicht der aktuell auswaehlbaren Kunden-Websites wie die Maske. Nur Admin."),
    ("wordpress.capabilities.list", capabilities, (Field("site_id", "Website-ID", required=True, max_length=18), OFFSET), "Gespeicherte Bridge-Faehigkeiten lesen; keine generelle Ausfuehrungserlaubnis."),
    ("wordpress.state.read", state, (Field("site_id", "Website-ID", required=True, max_length=18), OFFSET), "Gespeicherten Website-Snapshot ohne Zugangsdaten lesen."),
):
    register_query(HubQuery(key, description, fields, reader))
for key, reader, id_field in (
    ("wordpress.fleet.status", fleet_status, "run_id"),
    ("wordpress.updates.batch_status", batch_status, "batch_id"),
    ("wordpress.plugins.batch_status", partial(batch_status, installation=True), "batch_id"),
    ("wordpress.updates.complete_status", complete_status, "run_id"),
):
    register_query(HubQuery(key, "Persistierten Fortschritt wie in der Maske lesen. Lange Listen mit offset und pagination nachladen, kein Fernzugriff.",
        (Field(id_field, id_field, required=True, max_length=32), OFFSET), partial(status_query, reader=reader, id_field=id_field)))
for key, label, fields, callback in (
    ("wordpress.fleet.start", "Website-Sammelpruefung starten", (Field("site_ids", "Website-IDs", required=True, max_length=40000, encoding="JSON array"),
        Field("mode", "Pruefart", required=True, options=tuple((mode, mode) for mode in FleetRefreshService.manual_modes()))), start_fleet),
    ("wordpress.fleet.cancel", "Website-Sammelpruefung abbrechen", (Field("run_id", "Prueflauf-ID", required=True, max_length=18),), cancel_fleet),
    ("websites.remove_test_registration", "Leere Testregistrierung entfernen", (Field("site_id", "Website-ID", required=True, max_length=18),), remove_registration),
):
    register_operation(HubOperation(key=key, module="websites", label=label, description=label,
        input_guide="Konkrete IDs verwenden. Vorhandene Rechte- und Schutzpruefungen gelten; queued ist nicht abgeschlossen.",
        input_fields=lambda fields=fields: fields, preview_fields=tuple((f.name, f.label) for f in fields), execute=callback,
        result_fields=(("run_id", "Prueflauf-ID"), ("status", "Status")) if key.startswith("wordpress.fleet.") else ()))
