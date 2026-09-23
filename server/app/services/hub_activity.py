"""Compact, content-free activity records shared by all Hub modules."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.hub_activity_event import HubActivityEvent


MODULE_LABELS = {
    "account": "Account",
    "agent": "Hub-Agent",
    "calendar": "Kalender",
    "cases": "Fälle",
    "contacts": "Kontakte",
    "customers": "Kunden",
    "emails": "E-Mails",
    "finance-articles": "Artikel",
    "finance-invoices": "Rechnungen",
    "finance-offers": "Angebote",
    "finance-orders": "Aufträge",
    "finance-recurring-invoices": "Periodische Rechnungen",
    "leads": "Leads",
    "legal-terms": "AGBs",
    "pdf-templates": "PDF-Vorlagen",
    "sites": "Sites",
    "users": "Benutzer",
    "workflows": "Workflows",
    "wordpress": "WordPress Bridge",
}
FINANCE_MODULES = {"articles", "offers", "orders", "invoices", "recurring-invoices"}
RESOURCE_ROOTS = {
    "cases": "/cases",
    "contacts": "/contacts",
    "customers": "/customers",
    "finance-articles": "/finance/articles",
    "finance-invoices": "/finance/invoices",
    "finance-offers": "/finance/offers",
    "finance-orders": "/finance/orders",
    "finance-recurring-invoices": "/finance/recurring-invoices",
    "leads": "/leads",
    "sites": "/sites",
}


@dataclass
class ActivityRequestContext:
    actor: str
    path: str
    method: str
    recorded: bool = False
    actor_user_id: int | None = None
    actor_name: str | None = None
    origin: str = "web"


_request_context: ContextVar[ActivityRequestContext | None] = ContextVar("hub_activity_request", default=None)


def begin_activity_request(actor: str, path: str, method: str, *, user=None) -> Token:
    return _request_context.set(ActivityRequestContext(
        actor=actor[:64], path=path, method=method,
        actor_user_id=getattr(user, "id", None),
        actor_name=getattr(user, "display_name", None) or getattr(user, "username", None),
        origin="agent" if path.startswith(("/agent/", "/assistant/")) else "web",
    ))


def end_activity_request(token: Token) -> None:
    _request_context.reset(token)


def current_activity_request() -> ActivityRequestContext | None:
    return _request_context.get()


def module_and_resource_for_path(path: str) -> tuple[str, str | None]:
    parts = path.strip("/").split("/")
    if parts[0] == "finance" and len(parts) > 1 and parts[1] in FINANCE_MODULES:
        return f"finance-{parts[1]}", parts[2] if len(parts) > 2 and parts[2].isdigit() else None
    if parts[0] == "account":
        if len(parts) > 1 and parts[1] in {"legal-terms", "pdf-templates", "workflows", "users"}:
            return parts[1], parts[2] if len(parts) > 2 and parts[2].isdigit() else None
        return "account", None
    if parts[0] in {"customers", "sites", "contacts", "leads", "cases"}:
        return parts[0], parts[1] if len(parts) > 1 and parts[1].isdigit() else None
    if parts[0] in {"emails", "calendar", "users", "agent", "assistant"}:
        return "agent" if parts[0] == "assistant" else parts[0], None
    if parts[0] in {"mcp", "api"}:
        return "account", None
    return parts[0][:64] or "hub", None


def module_for_action(source: str, action: str, fallback_path: str | None = None) -> str:
    match = re.search(r"finance-(recurring-invoice|invoice|offer|order|article)", action)
    if match:
        return {
            "recurring-invoice": "finance-recurring-invoices",
            "invoice": "finance-invoices",
            "offer": "finance-offers",
            "order": "finance-orders",
            "article": "finance-articles",
        }[match.group(1)]
    for word, module in (("pdf-template", "pdf-templates"), ("legal-terms", "legal-terms"), ("workflow", "workflows")):
        if word in action or word in source:
            return module
    if fallback_path:
        return module_and_resource_for_path(fallback_path)[0]
    for word, module in (("customer", "customers"), ("site", "sites"), ("email", "emails"), ("user", "users")):
        if word in action or word in source:
            return module
    return "account" if source.startswith("hub-account") else source.removeprefix("hub-")[:64] or "hub"


def category_for_action(action: str) -> str:
    if action.startswith(("create", "add", "import")):
        return "create"
    if action.startswith(("update", "edit", "save", "change", "configure")):
        return "update"
    if action.startswith(("delete", "remove", "revoke")):
        return "delete"
    if action.startswith(("view", "read", "list", "download")):
        return "view"
    return "execute"


def record_audit_activity(
    db: Session, *, actor: str, source: str, action: str, result: str, detail: str | None, site_id: int | None
) -> None:
    context = current_activity_request()
    module_key = module_for_action(source, action, context.path if context else None)
    path_resource = module_and_resource_for_path(context.path)[1] if context else None
    detail_resource = re.search(
        r"\b(?:Offer|Invoice|Order|Article|Customer|Site|Template|Terms) (\d+)\b",
        detail or "", flags=re.IGNORECASE,
    )
    resource_id = path_resource or (detail_resource.group(1) if detail_resource else None)
    if module_key == "sites" and site_id:
        resource_id = str(site_id)
    db.add(HubActivityEvent(
        actor=actor[:64], module_key=module_key, resource_id=resource_id,
        category=category_for_action(action), action=action[:128], result=result[:24],
        origin="web" if context else "background", changed_fields=None,
    ))
    db.info.setdefault("hub_activity_manual_modules", set()).add(module_key)
    if context:
        context.recorded = True


def record_http_activity(
    db: Session, *, actor: str, path: str, method: str, status_code: int, route_path: str | None = None
) -> None:
    module_key, resource_id = module_and_resource_for_path(path)
    category = "view" if method in {"GET", "HEAD"} else "execute"
    db.add(HubActivityEvent(
        actor=actor[:64], module_key=module_key, resource_id=resource_id,
        category=category, action=f"{method} {route_path or 'request'}"[:128],
        result="error" if status_code >= 400 else "success", origin="web",
    ))
    db.commit()


def activity_resource_url(event: HubActivityEvent) -> str | None:
    if event.resource_id and event.resource_id.isdigit() and event.module_key in RESOURCE_ROOTS:
        url = f"{RESOURCE_ROOTS[event.module_key]}/{event.resource_id}"
    elif event.module_key in RESOURCE_ROOTS:
        url = RESOURCE_ROOTS[event.module_key]
    elif event.module_key in {"account", "legal-terms", "pdf-templates", "users"}:
        section = {"legal-terms": "legal-terms", "pdf-templates": "pdf-templates", "users": "users"}.get(event.module_key)
        url = f"/account#account-{section}" if section else "/account"
    else:
        return None
    path, separator, fragment = url.partition("#")
    return f"{path}?from_protocol=1{separator}{fragment}"


def is_protocol_resource_path(path: str) -> bool:
    """Only list/detail navigation, never downloads or action endpoints."""
    path = path.rstrip("/")
    if path in {"/account", "/settings"}:
        return True
    return any(
        path == root or (path.startswith(root + "/") and path[len(root) + 1:].isdigit())
        for root in RESOURCE_ROOTS.values()
    )


def list_activity_events(db: Session, params, *, page_size: int = 50) -> dict:
    module_key = (params.get("protocol_module") or "")[:64]
    actor = (params.get("protocol_actor") or "")[:64]
    category = (params.get("protocol_action") or "")[:24]
    start = (params.get("protocol_from") or "")[:10]
    end = (params.get("protocol_to") or "")[:10]
    try:
        page = max(1, min(int(params.get("protocol_page", "1")), 10000))
    except ValueError:
        page = 1
    query = select(HubActivityEvent)
    if module_key:
        query = query.where(HubActivityEvent.module_key == module_key)
    if actor:
        from app.models.hub_user import HubUser
        user_id = db.scalar(select(HubUser.id).where(HubUser.username == actor))
        query = query.where(or_(
            HubActivityEvent.actor_user_id == user_id if user_id is not None else False,
            (HubActivityEvent.actor_user_id.is_(None)) & (HubActivityEvent.actor == actor),
        ))
    if category:
        query = query.where(HubActivityEvent.category == category)
    try:
        if start:
            query = query.where(HubActivityEvent.timestamp >= datetime.combine(date.fromisoformat(start), time.min))
        if end:
            query = query.where(HubActivityEvent.timestamp < datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min))
    except ValueError:
        pass
    rows = db.scalars(query.order_by(HubActivityEvent.id.desc()).offset((page - 1) * page_size).limit(page_size + 1)).all()
    available_modules = db.scalars(select(HubActivityEvent.module_key).distinct().order_by(HubActivityEvent.module_key)).all()
    return {
        "rows": [(row, activity_resource_url(row)) for row in rows[:page_size]],
        "modules": {key: MODULE_LABELS.get(key, key) for key in sorted(set(MODULE_LABELS) | set(available_modules))},
        "page": page,
        "has_next": len(rows) > page_size,
        "filters": {"module": module_key, "actor": actor, "action": category, "from": start, "to": end},
    }
