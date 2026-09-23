"""Record business writes in the same transaction, including changes to line items."""
from datetime import UTC, datetime

from sqlalchemy import delete, event, inspect, select

from app.core.record_actor import resolve_record_actor
from app.models.hub_record_info import HubRecordInfo
from app.services.hub_activity import current_activity_request


# A model may also opt in with __record_info__ = True. Storage/cache tables stay out.
RECORD_TABLES = frozenset({
    "customers", "hub_leads", "customer_contacts", "hub_cases", "sites",
    "hub_finance_articles", "hub_finance_offers", "hub_finance_orders",
    "hub_finance_invoices", "hub_finance_dunnings", "hub_finance_recurring_invoices",
    "customer_call_activities", "customer_task_activities", "customer_meeting_activities",
    "customer_zoho_notes", "hub_lead_notes", "hub_pdf_templates", "hub_legal_terms",
    "hub_finance_position_presets", "hub_workflows", "hub_users",
})
PARENT_RECORDS = {
    "hub_finance_offer_lines": ("hub_finance_offers", "offer_id"),
    "hub_finance_order_lines": ("hub_finance_orders", "order_id"),
    "hub_finance_invoice_lines": ("hub_finance_invoices", "invoice_id"),
    "hub_finance_dunning_lines": ("hub_finance_dunnings", "dunning_id"),
    "hub_finance_recurring_invoice_lines": ("hub_finance_recurring_invoices", "recurring_invoice_id"),
    "customer_call_reminders": ("customer_call_activities", "call_id"),
    "customer_meeting_reminders": ("customer_meeting_activities", "meeting_id"),
    "hub_case_email_links": ("hub_cases", "case_id"),
}
ROUTINE_FIELDS = frozenset({
    "created_at", "updated_at", "last_seen_at", "last_login_at", "last_used_at",
    "last_synced_at", "zoho_modified_at", "zoho_created_at", "zoho_imported_at",
    "zoho_synced_at", "sync_status", "last_error", "last_reminder_sent_at",
    "reminder_sent_at", "last_snapshot_at", "last_inventory_at",
})


def is_imported(record):
    note_id = getattr(record, "zoho_note_id", None)
    # Native Lead notes also use this legacy column, with a Hub-generated key.
    native_lead_note = (getattr(record, "__tablename__", "") == "hub_lead_notes"
                        and str(note_id or "").startswith("hub-"))
    return bool(getattr(record, "zoho_id", None) or getattr(record, "zoho_books_id", None)
                or (note_id and not native_lead_note) or getattr(record, "source_external_id", None))


def _business_changed(record):
    state = inspect(record)
    if any(column.key not in ROUTINE_FIELDS and state.attrs[column.key].history.has_changes()
           for column in state.mapper.column_attrs):
        return True
    # delete-orphan collection removals are not yet in Session.deleted at before_flush.
    return any(PARENT_RECORDS.get(relation.mapper.local_table.name, (None,))[0] == record.__tablename__
               and state.attrs[relation.key].history.has_changes() for relation in state.mapper.relationships)


def _capture(db, _flush_context, _instances):
    from app.models.audit_log import AuditLog
    from app.models.hub_activity_event import HubActivityEvent
    for entry in tuple(db.new):
        if isinstance(entry, (AuditLog, HubActivityEvent)) and not entry.actor_name:
            actor = resolve_record_actor(db, entry.actor)
            entry.actor_user_id, entry.actor_name = actor.user_id, actor.name
            if isinstance(entry, HubActivityEvent) and actor.origin == "agent":
                entry.origin = "agent"
    request = current_activity_request()
    if request and request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    pending = []
    actor = None
    for kind, records in (("create", tuple(db.new)), ("update", tuple(db.dirty)), ("delete", tuple(db.deleted))):
        for record in records:
            table = getattr(record, "__tablename__", "")
            tracked = table in RECORD_TABLES or getattr(record, "__record_info__", False)
            if not tracked and table not in PARENT_RECORDS:
                continue
            if kind == "update" and not _business_changed(record):
                continue
            if kind == "create" and is_imported(record):
                continue
            actor = actor or resolve_record_actor(db)
            parent = PARENT_RECORDS.get(table)
            previous_parents = tuple(inspect(record).attrs[parent[1]].history.deleted) if parent else ()
            pending.append((record, table, kind, actor, previous_parents))
    db.info["record_info_pending"] = pending


def _upsert(connection, values):
    table = HubRecordInfo.__table__
    dialect = connection.dialect.name
    updates = {key: value for key, value in values.items() if key.startswith("changed_")}
    if dialect == "mysql":
        from sqlalchemy.dialects.mysql import insert
        statement = insert(table).values(**values)
        connection.execute(statement.on_duplicate_key_update(**updates))
    elif dialect in {"sqlite", "postgresql"}:
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        statement = insert(table).values(**values)
        connection.execute(statement.on_conflict_do_update(index_elements=["record_table", "record_id"], set_=updates))
    else:
        condition = (table.c.record_table == values["record_table"]) & (table.c.record_id == values["record_id"])
        if connection.scalar(select(table.c.record_id).where(condition)) is None:
            connection.execute(table.insert().values(**values))
        else:
            connection.execute(table.update().where(condition).values(**updates))


def _persist(db, _flush_context):
    changes = db.info.pop("record_info_pending", [])
    if not changes:
        return
    connection = db.connection()
    rows, deleted = {}, set()
    now = datetime.now(UTC)
    for record, table, kind, actor, previous_parents in changes:
        parent = PARENT_RECORDS.get(table)
        keys = [(table, record.id)] if parent is None else [
            (parent[0], parent_id) for parent_id in (*previous_parents, getattr(record, parent[1], None)) if parent_id]
        for key in keys:
            if not key[1]:
                continue
            if kind == "delete" and parent is None:
                deleted.add(key)
                continue
            values = rows.setdefault(key, {"record_table": key[0], "record_id": key[1]})
            values.update(changed_at=now, changed_user_id=actor.user_id, changed_name=actor.name, changed_origin=actor.origin)
            if kind == "create" and parent is None and not is_imported(record):
                values.update(created_at=now, created_user_id=actor.user_id, created_name=actor.name, created_origin=actor.origin)
    for key, values in rows.items():
        if key not in deleted:
            _upsert(connection, values)
    for table, identifier in deleted:
        connection.execute(delete(HubRecordInfo).where(HubRecordInfo.record_table == table, HubRecordInfo.record_id == identifier))
    db.info.pop("record_info_views", None)


def install_record_info_tracking(factory):
    event.listen(factory, "before_flush", _capture)
    event.listen(factory, "after_flush_postexec", _persist)
    event.listen(factory, "after_rollback", lambda db: db.info.pop("record_info_pending", None))
