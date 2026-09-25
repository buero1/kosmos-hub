"""Shared Finance record access and form adapters for UI and agent operations."""

from __future__ import annotations

from typing import Mapping

from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_offer import HubFinanceOffer
from app.models.hub_finance_documents import HubFinanceOrder
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import FINANCE_DOCUMENT_MODULES, HubFinanceDocumentService
from app.services.hub_finance_field_catalog import ARTICLE_FIELDS, OFFER_FIELDS
from app.services.hub_operations import HubOperationError
from app.services.hub_record_access import require_actor


FINANCE_KINDS = ("articles", "offers", *FINANCE_DOCUMENT_MODULES)
PDF_KINDS = ("offers", "orders", "invoices", "dunnings")
LINE_KEYS = ("article_id", "name", "sku", "description", "quantity", "unit", "unit_price", "discount_percent", "tax_rate", "delete")


def model_for(kind):
    if kind == "articles":
        return HubFinanceArticle
    if kind == "offers":
        return HubFinanceOffer
    if kind in FINANCE_DOCUMENT_MODULES:
        return FINANCE_DOCUMENT_MODULES[kind].model
    raise HubOperationError("Dieses Finance-Modul ist nicht verfuegbar.")


def fields_for(kind):
    if kind == "articles":
        return ARTICLE_FIELDS
    if kind == "offers":
        return OFFER_FIELDS
    return FINANCE_DOCUMENT_MODULES[kind].fields


def prefix_for(kind):
    return {"articles": "article", "offers": "offer"}.get(kind, "document")


def parent_visible(record, user, access):
    parents = [(module, getattr(record, attribute, None)) for module, attribute in (
        ("customers", "customer_id"), ("leads", "lead_id"),
    ) if getattr(record, attribute, None) is not None]
    # Only the creator can access their unassigned copy; old orphans stay admin-only.
    if not parents:
        return user.role == "admin" or (
            isinstance(record, (HubFinanceOffer, HubFinanceOrder)) and record.unassigned_owner_user_id == user.id
        )
    return all(access.can_access_record(user=user, module_key=module, record_id=record_id)
               for module, record_id in parents)


def require_record(service, kind, record_id, action="view"):
    user, access = require_actor(service, "finance", action)
    record = service.db.get(model_for(kind), record_id) if record_id else None
    if record is None or (kind != "articles" and not parent_visible(record, user, access)):
        raise HubOperationError("Der Finance-Datensatz ist nicht verfuegbar.")
    return record


def finance_detail(service, kind, record_id):
    require_record(service, kind, record_id)
    if kind == "articles":
        return HubFinanceService(db=service.db, cipher=service.cipher).get_article_detail(article_id=record_id)
    if kind == "offers":
        return HubFinanceService(db=service.db, cipher=service.cipher).get_offer_detail(offer_id=record_id)
    return HubFinanceDocumentService(db=service.db, cipher=service.cipher).get_detail(
        module=FINANCE_DOCUMENT_MODULES[kind], document_id=record_id,
    )


def finance_entries(service, kind):
    model_for(kind)
    user, access = require_actor(service, "finance", "view")
    if kind == "articles":
        return HubFinanceService(db=service.db, cipher=service.cipher).list_articles()
    if kind == "offers":
        entries = HubFinanceService(db=service.db, cipher=service.cipher).list_offers()
        return tuple(entry for entry in entries if parent_visible(entry.offer, user, access))
    entries = HubFinanceDocumentService(db=service.db, cipher=service.cipher).list_documents(module=FINANCE_DOCUMENT_MODULES[kind])
    return tuple(entry for entry in entries if parent_visible(entry.document, user, access))


def finance_invoice_page(service, page):
    user, access = require_actor(service, "finance", "view")
    allowed = access.accessible_record_ids(user=user, module_key="customers")
    return HubFinanceDocumentService(db=service.db, cipher=service.cipher).list_invoice_page(
        page=page, allowed_customer_ids=allowed, include_orphans=user.role == "admin",
    )


def finance_options(service, kind):
    model_for(kind)
    user, access = require_actor(service, "finance", "view")
    domain = HubFinanceService(db=service.db, cipher=service.cipher)
    customers = tuple(item for item in domain.list_linkable_customers()
                      if access.can_access_record(user=user, module_key="customers", record_id=item.id))
    allowed_customers = {item.id for item in customers}
    contacts = tuple(item for item in domain.list_linkable_contacts() if item.customer_id in allowed_customers)
    leads = tuple(item for item in domain.list_linkable_leads()
                  if access.can_access_record(user=user, module_key="leads", record_id=item.id)) if kind == "offers" else ()
    links = ()
    if kind in FINANCE_DOCUMENT_MODULES:
        links = tuple(item for item in HubFinanceDocumentService(db=service.db, cipher=service.cipher).link_options(
            module=FINANCE_DOCUMENT_MODULES[kind],
        ) if item.customer_id in allowed_customers)
        if kind == "orders":
            from app.services.hub_finance_documents import FinanceDocumentLinkOption
            links = tuple(FinanceDocumentLinkOption(entry.offer.id, entry.offer_number, entry.offer.customer_id)
                          for entry in domain.list_offers() if parent_visible(entry.offer, user, access))
    return {"customers": customers, "contacts": contacts, "leads": leads, "link_options": links}


def stored_values(service, kind, record):
    prefix = prefix_for(kind)
    domain = HubFinanceService(db=service.db, cipher=service.cipher) if kind in {"articles", "offers"} else HubFinanceDocumentService(db=service.db, cipher=service.cipher)
    raw = domain._values(record.encrypted_fields_json) if kind in {"articles", "offers"} else domain._document_values(module=FINANCE_DOCUMENT_MODULES[kind], document=record)
    if kind == "offers":
        raw["notes"] = domain.offer_notes(raw)
    result = {f"{prefix}_field__{key}": str(value) for key, value in raw.items()}
    if kind == "recurring-invoices":
        count, unit = domain._payment_due_parts(raw)
        result.update(document_field__payment_due_count=count, document_field__payment_due_unit=unit)
    if kind != "articles":
        for index, line in enumerate(record.lines):
            values = domain._values(line.encrypted_fields_json)
            values["article_id"] = str(line.article_id or "")
            result.update({f"{prefix}_line__{index}__{key}": str(value) for key, value in values.items()})
    return result


def form_defaults(kind, values: Mapping[str, str]):
    if kind == "articles":
        return HubFinanceService.new_article_values()
    if kind == "offers":
        defaults = HubFinanceService.new_offer_values(offer_date=values.get("offer_field__offer_date"))
    else:
        defaults = HubFinanceDocumentService.new_form_values(module=FINANCE_DOCUMENT_MODULES[kind])
    prefix = prefix_for(kind)
    line_defaults = {key.rsplit("__", 1)[1]: value for key, value in defaults.items() if key.startswith(f"{prefix}_line__0__")}
    indices = {int(parts[1]) for key in values if len(parts := key.split("__")) == 3 and parts[0] == f"{prefix}_line" and parts[1].isdigit()}
    for index in indices:
        defaults.update({f"{prefix}_line__{index}__{key}": value for key, value in line_defaults.items()})
    return defaults


def merge_form(service, kind, values, record=None):
    """Omission preserves data; only explicit replacement discards unsubmitted rows."""
    prefix = prefix_for(kind)
    mode = values.get("lines_mode", "patch")
    if mode not in {"patch", "replace"}:
        raise HubOperationError("Ungueltiger Positionsmodus.")
    field_keys = {field.key for field in fields_for(kind) if not field.read_only and field.display_type != "Verknuepfung"}
    field_keys.update({"custom_interval_count", "custom_interval_unit", "payment_due_count", "payment_due_unit"} if kind == "recurring-invoices" else ())
    controls = {"record_id"}
    if kind != "articles":
        controls.update({"customer_id", "customer_name", "contact_id", "pdf_template_id", "lines_mode"})
    if kind == "offers":
        controls.update({"lead_id", "lead_name"})
    elif kind in FINANCE_DOCUMENT_MODULES and FINANCE_DOCUMENT_MODULES[kind].link_key:
        controls.add("linked_record_id")
    for key in values:
        if key not in controls and not key.startswith((f"{prefix}_field__", f"{prefix}_line__")):
            raise HubOperationError("Unbekannte Finance-Eingabe.")
        if key.startswith(f"{prefix}_field__") and key.removeprefix(f"{prefix}_field__") not in field_keys:
            raise HubOperationError("Ein Feld ist unbekannt oder schreibgeschuetzt.")
        if key.startswith(f"{prefix}_line__"):
            parts = key.split("__")
            if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in LINE_KEYS or int(parts[1]) > 999:
                raise HubOperationError("Ungueltige Positionseingabe.")
    previous = stored_values(service, kind, record) if record is not None else {}
    if mode == "replace":
        previous = {key: value for key, value in previous.items() if not key.startswith(f"{prefix}_line__")}
    defaults = form_defaults(kind, values)
    if record is not None:
        # New row defaults only: never reset existing fields or lines on a partial edit.
        defaults = {key: value for key, value in defaults.items() if key.startswith(f"{prefix}_line__") and any(
            submitted.startswith(key.rsplit("__", 1)[0] + "__") for submitted in values
        )}
    return {**defaults, **previous, **{key: value for key, value in values.items() if key.startswith((f"{prefix}_field__", f"{prefix}_line__"))}}


def form_input(form, *, kind, record_id=None):
    prefix = prefix_for(kind)
    values = {str(key): str(value) for key, value in form.items() if isinstance(value, str) and str(key).startswith((f"{prefix}_field__", f"{prefix}_line__"))}
    # Read-only controls can be present in HTML but are never submitted as mutations.
    for field in fields_for(kind):
        if field.read_only or field.display_type == "Verknuepfung":
            values.pop(f"{prefix}_field__{field.key}", None)
    if kind != "articles":
        values.update({key: str(form.get(key) or "") for key in ("customer_id", "contact_id", "pdf_template_id")})
        values["lines_mode"] = "replace"
        if kind == "offers":
            values["lead_id"] = str(form.get("lead_id") or "")
        elif FINANCE_DOCUMENT_MODULES[kind].link_key:
            values["linked_record_id"] = str(form.get("linked_record_id") or "")
    if record_id is not None:
        values["record_id"] = str(record_id)
    return values
