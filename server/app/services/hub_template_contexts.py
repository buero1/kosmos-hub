"""Authorized template values from the same field catalogs and readers as the UI."""

from datetime import UTC, datetime
import re
from zoneinfo import ZoneInfo
from sqlalchemy import select

from app.models.hub_lead_conversion import HubLeadConversion
from app.models.site import Site
from app.services.customer_activities import CALL_STATUS_OPTIONS, CALL_DIRECTION_OPTIONS, CALL_REMINDER_CHANNEL_OPTIONS
from app.services.hub_crm_readers import HubCrmReadService
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_operations_shared import FINANCE_DOCUMENT_MODULES, finance_detail
from app.services.hub_operation_records import customer_detail, lead_detail
from app.services.hub_operations import HubOperationError
from app.services.hub_record_access import identifier, require_actor
from app.services.template_placeholders import (
    EMAIL_TEMPLATE_CONTEXT_KEYS, LEAD_PLACEHOLDERS, CASE_PLACEHOLDERS,
    RECURRING_INVOICE_PLACEHOLDERS, TASK_PLACEHOLDERS, CALL_PLACEHOLDERS,
    MEETING_PLACEHOLDERS, SITE_PLACEHOLDERS, DOCUMENT_NAMES,
    email_document_placeholders, contact_greeting, company_template_values,
)


def display(value):
    if value is None or value == "-":
        return ""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    return str(value)


def catalog_values(definitions, values):
    return {item.token[2:-1]: display(values.get(item.profile_key)) for item in definitions if item.profile_key}


def context_from_path(path):
    match = re.fullmatch(r"/(customers|leads|cases|sites)/(\d+)", path)
    if not match:
        match = re.fullmatch(r"/finance/(offers|orders|invoices|dunnings|recurring-invoices)/(\d+)", path)
    if match:
        return match.groups()
    match = re.fullmatch(r"/activities/(call|task|meeting)/(\d+)", path)
    return (match[1] + "s", match[2]) if match else ("", "")


def resolve_context(service, values):
    user, access = require_actor(service, "emails", "view")
    inputs = dict(values)
    kind, record_id = inputs.get("context_module", ""), inputs.get("context_record_id", "")
    if not kind and not record_id:
        if inputs.get("dunning_id"):
            kind, record_id = "dunnings", inputs["dunning_id"]
        else:
            kind, record_id = context_from_path(inputs.get("context_path", ""))
            if not kind and inputs.get("lead_id"):
                kind, record_id = "leads", inputs["lead_id"]
    if kind and kind not in EMAIL_TEMPLATE_CONTEXT_KEYS or bool(record_id) != bool(kind and kind != "general"):
        raise HubOperationError("Vorlagenkontext und Datensatz-ID muessen zusammenpassen.")
    tokens = company_template_values()
    if record_id:
        additions, customer, lead = record_values(service, kind, identifier(record_id))
        for key, parent in (("customer_id", customer), ("lead_id", lead)):
            explicit = identifier(inputs.get(key, ""))
            if explicit is not None and explicit != parent:
                raise HubOperationError("Der Vorlagenkontext gehoert nicht zum ausgewaehlten Kunden oder Lead.")
            if parent is not None:
                inputs[key] = str(parent)
        tokens.update(additions)
        inputs.update(context_module=kind, context_record_id=record_id)
        if kind == "dunnings":
            inputs["dunning_id"] = record_id
    for module, key in (("customers", "customer_id"), ("leads", "lead_id")):
        parent = identifier(inputs.get(key, ""))
        if parent and not access.can_access_record(user=user, module_key=module, record_id=parent):
            raise HubOperationError("Der verknuepfte Datensatz ist nicht verfuegbar.")
    return inputs, tokens


def record_values(service, kind, record_id):
    crm = HubCrmReadService(db=service.db, cipher=service.cipher, actor=service.actor)
    if kind == "customers":
        customer_detail(service, {"customer_id": str(record_id)})
        return {}, record_id, None
    if kind == "leads":
        detail = lead_detail(service, {"lead_id": str(record_id)})
        fields = {field.key: display(field.value) for field in detail.fields}
        tokens = catalog_values(LEAD_PLACEHOLDERS, fields)
        tokens["Lead.Greeting"] = contact_greeting(name=detail.name, salutation=fields.get("salutation", ""),
            last_name=fields.get("last_name", ""), letter_salutation=fields.get("letter_salutation", ""))
        return tokens, None, record_id
    if kind == "cases":
        detail = crm.case_detail(record_id)
        return catalog_values(CASE_PLACEHOLDERS, {field.key: field.value for field in detail.fields}), detail.case.customer_id, None
    if kind in {"tasks", "calls", "meetings"}:
        row = crm.activity_record(kind[:-1], record_id)
        definitions = {"tasks": TASK_PLACEHOLDERS, "calls": CALL_PLACEHOLDERS, "meetings": MEETING_PLACEHOLDERS}[kind]
        tokens = {}
        for item in definitions:
            suffix = item.token[2:-1].split(".")[1]
            attribute = re.sub(r"(?<!^)(?=[A-Z])", "_", suffix).lower()
            attribute = {"title": "name", "created_by": "created_by_username"}.get(attribute, attribute)
            value = getattr(row, attribute, None)
            options = {"status": CALL_STATUS_OPTIONS, "direction": CALL_DIRECTION_OPTIONS, "reminder_channel": CALL_REMINDER_CHANNEL_OPTIONS}.get(attribute)
            if options:
                value = dict(options).get(value, value)
            reminders = getattr(row, "reminders", ())
            if reminders and suffix in {"ReminderChannel", "ReminderMinutesBefore"}:
                value = ", ".join(display(dict(CALL_REMINDER_CHANNEL_OPTIONS).get(r.channel, r.channel) if suffix == "ReminderChannel" else r.minutes_before) for r in reminders)
            tokens[item.token[2:-1]] = display(value)
        if row.lead_id:
            tokens.update(record_values(service, "leads", row.lead_id)[0])
        if getattr(row, "case_id", None):
            tokens.update(record_values(service, "cases", row.case_id)[0])
        return tokens, row.customer_id, row.lead_id
    if kind == "sites":
        user, access = require_actor(service, "websites", "view")
        row = service.db.get(Site, record_id)
        if row is None or row.customer_id and not access.can_access_record(user=user, module_key="customers", record_id=row.customer_id):
            raise HubOperationError("Die Website ist nicht verfuegbar.")
        return {item.token[2:-1]: display(getattr(row, re.sub(r"(?<!^)(?=[A-Z])", "_", item.token[7:-1]).lower(), None))
                for item in SITE_PLACEHOLDERS}, row.customer_id, None
    if kind in {"offers", *FINANCE_DOCUMENT_MODULES}:
        detail = finance_detail(service, kind, record_id)
        row = detail.offer if kind == "offers" else detail.document
        fields = {field.key: field for field in detail.fields}
        definitions = RECURRING_INVOICE_PLACEHOLDERS if kind == "recurring-invoices" else email_document_placeholders(kind)
        tokens = catalog_values(definitions, {key: field.value for key, field in fields.items()})
        if kind != "recurring-invoices":
            namespace = DOCUMENT_NAMES[kind][0]
            tokens.update({f"{namespace}.Number": detail.offer_number if kind == "offers" else detail.identifier,
                f"{namespace}.Status": detail.status})
            tokens.setdefault(f"{namespace}.Title", {"offers": "Angebot", "orders": "Auftrag", "invoices": "Rechnung", "dunnings": "Mahnung"}[kind])
            currency = fields["currency"].form_value if fields.get("currency") else "EUR"
            for name, value in (("NetTotal", detail.totals.subtotal_net), ("TaxTotal", detail.totals.tax_total), ("GrossTotal", detail.totals.total_gross)):
                tokens[f"{namespace}.{name}"] = HubFinanceService.format_money(value, currency or "EUR")
        module = FINANCE_DOCUMENT_MODULES.get(kind)
        linked = getattr(row, module.link_attribute, None) if module and module.link_attribute else None
        if linked is not None:
            related, parent, related_lead = record_values(service, module.link_attribute + "s", linked.id)
            converted_offer = kind == "orders" and parent is None and related_lead is not None and row.customer_id is not None and service.db.scalar(
                select(HubLeadConversion.id).where(HubLeadConversion.lead_id == related_lead,
                                                   HubLeadConversion.customer_id == row.customer_id)
            ) is not None
            if parent != row.customer_id and not converted_offer:
                raise HubOperationError("Der verknuepfte Beleg gehoert zu einem anderen Kunden.")
            tokens.update(related)
        lead = getattr(row, "lead_id", None)
        if lead:
            tokens.update(record_values(service, "leads", lead)[0])
        return tokens, row.customer_id, lead
    raise HubOperationError("Dieser Vorlagenkontext ist nicht verfuegbar.")
