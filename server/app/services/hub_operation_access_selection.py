"""Named record selection and atomic bulk access changes shared by UI and agent."""
from functools import partial
import json

from sqlalchemy import select

from app.models.customer import Customer
from app.models.hub_lead import HubLead
from app.models.hub_user import HubUser
from app.models.hub_access_control import HubRecordAssignment, HubTeam
from app.services.audit import write_audit_log
from app.services.hub_administration import HubAdministrationService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_operation_records import customer_entries, lead_entries
from app.services.hub_operations import (
    HubOperation, HubOperationError, HubOperationInputField as Field,
    HubOperationResult, HubQuery, register_operation, register_query,
)


MAX_SELECTION = 5000
MODULES = {"customers": Customer, "leads": HubLead}
MODULE_FIELD = Field("module_key", "Modul", required=True, options=(("customers", "Kunden"), ("leads", "Leads")))
IDS_FIELD = Field("record_ids", "Ausgewaehlte Datensaetze", required=True, max_length=120000,
                  encoding="JSON array of record ID strings; up to 5000 IDs, empty only with previous_record_ids for team editing")


def admin(service):
    return HubAdministrationService(db=service.db, cipher=service.cipher, actor=service.actor).user()


def module_model(values):
    model = MODULES.get(values.get("module_key"))
    if model is None:
        raise HubOperationError("Bitte Kunden oder Leads auswaehlen.")
    return model


def positive_id(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) < 1:
        raise HubOperationError("Ungueltige Auswahl. Bitte die Datensaetze erneut auswaehlen.")
    return int(value)


def selection_ids(raw, *, allow_empty=False):
    try:
        selected = json.loads(raw)
    except ValueError as exc:
        raise HubOperationError("Bitte Datensaetze auswaehlen.") from exc
    if not isinstance(selected, list) or not (0 if allow_empty else 1) <= len(selected) <= MAX_SELECTION:
        raise HubOperationError("Bitte hoechstens 5000 Datensaetze auswaehlen; eine leere Auswahl ist nur beim Bearbeiten eines Teams erlaubt.")
    return sorted({positive_id(value) for value in selected})


def options(service, values):
    admin(service)
    module_model(values)
    module = values["module_key"]
    team_id = positive_id(values["team_id"]) if values.get("team_id") else None
    selected_ids = set()
    if team_id is not None:
        team = service.db.get(HubTeam, team_id, populate_existing=True)
        if team is None or not team.is_active:
            raise HubOperationError("Das ausgewaehlte Team ist nicht mehr aktiv.")
        selected_ids = set(service.db.scalars(select(HubRecordAssignment.record_id).where(
            HubRecordAssignment.module_key == module, HubRecordAssignment.team_id == team_id)))
        if len(selected_ids) > MAX_SELECTION:
            raise HubOperationError("Dieses Team hat mehr als 5000 Zuweisungen. Bitte die Auswahl gezielt aufteilen.")
    raw_limit, raw_offset = values.get("limit") or "100", values.get("offset") or "0"
    if not raw_offset.isascii() or not raw_offset.isdecimal() or len(raw_offset) > 7:
        raise HubOperationError("Ungueltiger Listenbeginn.")
    limit, offset = positive_id(raw_limit), int(raw_offset)
    if limit > MAX_SELECTION:
        raise HubOperationError("Bitte die Suche auf hoechstens 5000 Treffer eingrenzen.")
    needle = values.get("query", "").strip().casefold()
    items = []
    if module == "customers":
        for entry in customer_entries(service):
            items.append({"record_id": str(entry.customer.id), "name": entry.customer.name,
                          "detail": entry.customer.website_domain or "", "status": entry.account_status or ""})
    else:
        for entry in lead_entries(service):
            items.append({"record_id": str(entry.lead.id), "name": entry.name,
                          "detail": " / ".join(filter(None, (entry.company, entry.email))), "status": entry.status or ""})
    # Keep assigned, hidden customers editable instead of silently dropping their assignments.
    if module == "customers" and selected_ids:
        missing = selected_ids - {int(item["record_id"]) for item in items}
        for customer in service.db.scalars(select(Customer).where(Customer.id.in_(missing))):
            items.append({"record_id": str(customer.id), "name": customer.name,
                          "detail": customer.website_domain or "", "status": "Ausgeblendet"})
    selected_items = [item for item in items if int(item["record_id"]) in selected_ids]
    items = sorted((item for item in items if not needle or needle in " ".join(item.values()).casefold()),
                   key=lambda item: (item["name"].casefold(), int(item["record_id"])))
    visible = items[offset:offset + limit]
    ids = {int(item["record_id"]) for item in visible} | selected_ids
    assignments = {row.record_id: row for row in service.db.scalars(select(HubRecordAssignment).where(
        HubRecordAssignment.module_key == module, HubRecordAssignment.record_id.in_(ids)))}
    users = dict(service.db.execute(select(HubUser.id, HubUser.username)).all())
    teams = dict(service.db.execute(select(HubTeam.id, HubTeam.name)).all())
    for item in {item["record_id"]: item for item in visible + selected_items}.values():
        row = assignments.get(int(item["record_id"]))
        item.update(owner=users.get(row.owner_user_id, "") if row else "",
                    team=teams.get(row.team_id, "") if row else "", url=f"/{module}/{item['record_id']}")
    result = {"items": visible, "total": len(items),
              "next_offset": str(offset + limit) if len(items) > offset + limit else ""}
    if team_id is not None:
        result.update(previous_record_ids=[str(record_id) for record_id in sorted(selected_ids)], selected_items=selected_items)
    return result


def fields(kind):
    return (MODULE_FIELD, IDS_FIELD, Field("team_id", "Team (ausgelassen: unveraendert; leer: entfernen)"),
            *((Field("owner_user_id", "Verantwortlicher (ausgelassen: unveraendert; leer: entfernen)"),
               Field("previous_record_ids", "Geladene Teamzuweisungen", max_length=120000,
                     encoding="Optional JSON ID array from access.records.options with team_id. Reconciles that team's selection, including deselections; reject stale snapshot. Omit for additive assignment.")) if kind == "assign" else
              (Field("user_id", "Benutzer, alternativ zum Team"), Field("can_edit", "Bearbeitung einschliessen", options=(("true", "Ja"), ("false", "Nein"))))))


def update_many(service, values, *, kind):
    actor = admin(service)
    allowed = {field.name: field for field in fields(kind)}
    if set(values) - allowed.keys() or any(not isinstance(v, str) or len(v) > (allowed[k].max_length or 255) for k, v in values.items()):
        raise HubOperationError("Unbekannte oder ungueltige Zuweisung.")
    model = module_model(values)
    reconcile = kind == "assign" and "previous_record_ids" in values
    ids = selection_ids(values.get("record_ids", "[]"), allow_empty=reconcile)
    previous = selection_ids(values["previous_record_ids"], allow_empty=True) if reconcile else []
    targets = {}
    for key, target in (("owner_user_id", HubUser), ("user_id", HubUser), ("team_id", HubTeam)):
        if key not in values:
            continue
        targets[key] = positive_id(values[key]) if values[key] else None
        if targets[key] is not None:
            row = service.db.get(target, targets[key], populate_existing=True)
            if row is None or not row.is_active:
                raise HubOperationError("Der ausgewaehlte Benutzer oder das Team ist nicht mehr aktiv.")
    if kind == "assign" and not targets:
        raise HubOperationError("Bitte einen Verantwortlichen oder ein Team zum Aendern waehlen.")
    if reconcile and targets.get("team_id") is None:
        raise HubOperationError("Bitte ein bestimmtes Team waehlen, um dessen Auswahl zu bearbeiten.")
    if kind == "grant":
        if (targets.get("user_id") is None) == (targets.get("team_id") is None):
            raise HubOperationError("Bitte genau einen Benutzer oder ein Team waehlen.")
        if values.get("can_edit", "false") not in {"true", "false"}:
            raise HubOperationError("Ungueltiges Bearbeitungsrecht.")
    access = HubAccessControlService(db=service.db)
    with service.db.begin_nested():
        if reconcile:
            service.db.scalar(select(HubTeam).where(HubTeam.id == targets["team_id"]).with_for_update())
        affected = sorted(set(ids) | set(previous))
        found = set(service.db.scalars(select(model.id).where(model.id.in_(affected)).order_by(model.id).with_for_update()))
        if found != set(affected):
            raise HubOperationError("Mindestens ein ausgewaehlter Datensatz existiert nicht mehr. Es wurde nichts geaendert.")
        existing = {row.record_id: row for row in service.db.scalars(select(HubRecordAssignment).where(
            HubRecordAssignment.module_key == values["module_key"], HubRecordAssignment.record_id.in_(affected))
            .with_for_update().execution_options(populate_existing=True))}
        removed = set(previous) - set(ids)
        if reconcile:
            current = set(service.db.scalars(select(HubRecordAssignment.record_id).where(
                HubRecordAssignment.module_key == values["module_key"], HubRecordAssignment.team_id == targets["team_id"])))
            if current != set(previous):
                raise HubOperationError("Die Teamzuweisungen wurden inzwischen geaendert. Bitte die Seite neu laden und die Auswahl erneut pruefen. Es wurde nichts gespeichert.")
            # Only detach explicit deselections from this snapshot, never unrelated owners or grants.
            for record_id in sorted(removed):
                row = existing[record_id]
                access.assign_record(module_key=values["module_key"], record_id=record_id,
                                     owner_user_id=row.owner_user_id, team_id=None)
        for record_id in ids:
            if kind == "assign":
                row = existing.get(record_id)
                access.assign_record(module_key=values["module_key"], record_id=record_id,
                    owner_user_id=targets.get("owner_user_id", row.owner_user_id if row else None),
                    team_id=targets.get("team_id", row.team_id if row else None))
            else:
                access.add_grant(module_key=values["module_key"], record_id=record_id,
                    user_id=targets.get("user_id"), team_id=targets.get("team_id"), can_edit=values.get("can_edit") == "true")
        write_audit_log(service.db, site=None, actor=actor.username, source="hub-access", action=f"{kind}-records", result="success",
                        detail=json.dumps({"module": values["module_key"], "record_ids": ids, **targets,
                                           **({"removed_record_ids": sorted(removed)} if reconcile else {}),
                                           **({"can_edit": values.get("can_edit") == "true"} if kind == "grant" else {})}))
        service.db.flush()
    label = f"{len(ids)} Datensaetze gespeichert" + (f", {len(removed)} Teamzuweisungen entfernt" if reconcile else "")
    return HubOperationResult(label, "/settings#account-access", 0, outputs={"changed": str(len(ids) + len(removed))})


register_query(HubQuery("access.records.options", "Kunden/Leads fuer Zuweisungen mit Namen, Status und vorhandenen Zuweisungen. Nur aktive Administratoren. Standard 100 pro Seite, maximal 5000; next_offset beachten.",
    (MODULE_FIELD, Field("query", "Suchtext", max_length=100), Field("offset", "Listenbeginn"), Field("limit", "Seitengroesse"),
     Field("team_id", "Optional: gespeicherte Team-Auswahl vollstaendig und unabhaengig von Suche/Seite mitladen")), options))
for kind, key in (("assign", "access.records.assign_many"), ("grant", "access.grants.create_many")):
    register_operation(HubOperation(key=key, module="settings", label="Mehrere Datensaetze " + ("zuweisen" if kind == "assign" else "freigeben"),
        description="Nur Admin. Alle ausgewaehlten Datensaetze gemeinsam speichern oder bei einem Fehler nichts aendern.",
        input_guide="IDs aus access.records.options. assign_many: ausgelassene owner_user_id/team_id bleiben unveraendert; explizit leer entfernt die Zuweisung. Bestehende Teamzuweisungen werden ersetzt. Zum Bearbeiten der gesamten Team-Auswahl: options mit team_id lesen und previous_record_ids unveraendert mitgeben; record_ids ist die neue Auswahl (auch leer). Abgewaehlte verlieren nur diese Teamzuweisung. Veraltete Auswahl wird abgelehnt. Ohne previous_record_ids bleibt die Operation additiv. create_many: genau user_id oder team_id, can_edit standardmaessig false. Erst nach Bestaetigung.",
        preview_fields=(("module_key", "Modul"), ("record_ids", "Datensaetze"), ("team_id", "Team"), ("owner_user_id", "Verantwortlicher"), ("user_id", "Freigabe fuer")),
        input_fields=partial(fields, kind), execute=partial(update_many, kind=kind), result_fields=(("changed", "Anzahl gespeicherter Datensaetze"),)))
