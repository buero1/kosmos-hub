"""Record un-audited ORM writes once per module and transaction."""

from collections import Counter

from sqlalchemy import event, inspect

from app.models.audit_log import AuditLog
from app.models.hub_activity_event import HubActivityEvent
from app.services.hub_activity import current_activity_request


_IGNORED_TABLES = {"request_nonces", "hub_setup_tokens", "hub_record_info"}
_SENSITIVE_FIELD_PARTS = (
    "password", "secret", "token", "credential", "content", "body", "html", "payload",
    "attachment", "image", "storage", "raw", "encrypted", "digest", "key", "oauth", "pdf",
)
_ROUTINE_FIELDS = {
    "updated_at", "last_seen_at", "last_used_at", "last_login_at",
    "last_synced_at", "last_success_at", "last_imap_uid", "alerted_at", "consecutive_failures",
}


def _module_for_object(obj) -> str | None:
    table = obj.__tablename__
    if table in _IGNORED_TABLES:
        return None
    if table == "hub_finance_generated_pdfs":
        document_type = getattr(obj, "document_type", "")
        return {
            "offers": "finance-offers", "orders": "finance-orders", "invoices": "finance-invoices",
        }.get(document_type, "finance-invoices")
    if table == "hub_finance_position_presets":
        context = current_activity_request()
        if context and context.path.startswith("/finance/"):
            return "finance-" + context.path.split("/")[2]
        return "finance-invoices" if getattr(obj, "library_key", "") == "invoices" else "finance-offers"
    for name, module in (
        ("hub_finance_recurring_invoice", "finance-recurring-invoices"),
        ("hub_finance_invoice", "finance-invoices"),
        ("hub_finance_order", "finance-orders"),
        ("hub_finance_offer", "finance-offers"),
        ("hub_finance_article", "finance-articles"),
        ("hub_pdf_template", "pdf-templates"),
        ("hub_legal_terms", "legal-terms"),
        ("hub_workflow", "workflows"),
        ("hub_mailbox", "emails"),
        ("customer_zoho_email", "emails"),
        ("customer_email", "emails"),
        ("email_", "emails"),
        ("zoho_email", "emails"),
        ("customer_call", "calendar"),
        ("customer_task", "calendar"),
        ("customer_meeting", "calendar"),
        ("customer_contact", "contacts"),
        ("customer", "customers"),
        ("hub_case", "cases"),
        ("hub_lead", "leads"),
        ("hub_user", "users"),
        ("site", "sites"),
        ("hub_agent", "agent"),
    ):
        if table.startswith(name):
            return module
    return table.removeprefix("hub_").removesuffix("s").replace("_", "-")[:64]


def _record_id(obj) -> str | None:
    for field in ("document_id", "recurring_invoice_id", "invoice_id", "order_id", "offer_id", "customer_id", "site_id", "id"):
        value = getattr(obj, field, None)
        if isinstance(value, int) and value > 0:
            return str(value)
    return None


def _changed_fields(obj) -> set[str]:
    state = inspect(obj)
    changed = {
        column.key for column in state.mapper.column_attrs
        if column.key not in _ROUTINE_FIELDS and state.attrs[column.key].history.has_changes()
    }
    visible = {field for field in changed if not any(part in field for part in _SENSITIVE_FIELD_PARTS)}
    if changed and not visible:
        visible.add("Geschützte Daten")
    return visible


def _capture_changes(session) -> None:
    changes = session.info.setdefault("hub_activity_changes", {})
    seen = session.info.setdefault("hub_activity_seen", set())
    for category, objects in (("create", session.new), ("update", session.dirty), ("delete", session.deleted)):
        for obj in objects:
            if isinstance(obj, (AuditLog, HubActivityEvent)):
                continue
            module = _module_for_object(obj)
            if module is None:
                continue
            fields = _changed_fields(obj) if category == "update" else set()
            if category == "update" and not fields:
                continue
            marker = (id(obj), category)
            if marker in seen:
                continue
            seen.add(marker)
            item = changes.setdefault(module, {"counts": Counter(), "resources": set(), "fields": set()})
            item["counts"][category] += 1
            resource_id = _record_id(obj)
            if resource_id and len(item["resources"]) < 2:
                item["resources"].add(resource_id)
            if len(item["fields"]) < 20:
                item["fields"].update(fields)


def _emit_activity(session) -> None:
    _capture_changes(session)
    context = current_activity_request()
    manual_modules = session.info.get("hub_activity_manual_modules", set())
    for module, change in session.info.get("hub_activity_changes", {}).items():
        if module in manual_modules:
            continue
        counts = change["counts"]
        total = sum(counts.values())
        category = next(iter(counts)) if total == 1 else "bulk"
        resources = change["resources"]
        session.add(HubActivityEvent(
            actor=context.actor if context else "system",
            module_key=module,
            resource_id=next(iter(resources)) if total == 1 and len(resources) == 1 else None,
            category=category,
            action=f"{category}:{total}" if total > 1 else category,
            result="success",
            origin="web" if context else "background",
            changed_fields=", ".join(sorted(change["fields"])) or None,
        ))
        if context:
            context.recorded = True


def _clear_activity(session) -> None:
    for key in ("hub_activity_changes", "hub_activity_seen", "hub_activity_manual_modules"):
        session.info.pop(key, None)


def _after_flush(session, _flush_context) -> None:
    _capture_changes(session)


def install_activity_tracking(session_factory) -> None:
    event.listen(session_factory, "after_flush", _after_flush)
    event.listen(session_factory, "before_commit", _emit_activity)
    event.listen(session_factory, "after_commit", _clear_activity)
    event.listen(session_factory, "after_rollback", _clear_activity)
