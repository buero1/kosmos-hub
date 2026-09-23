"""Shared versioned PDF-template and legal-terms administration."""
from functools import partial
import json

from sqlalchemy import select

from app.models.hub_legal_terms import HubLegalTerms
from app.models.hub_pdf_template import HubPdfTemplate
from app.services.hub_document_template_catalog import pdf_fields, legal_fields
from app.services.hub_legal_terms import HubLegalTermsService
from app.services.hub_pdf_templates import HubPdfTemplateService, PDF_LINE_SOURCES, PDF_TEMPLATE_TYPES
from app.services.hub_operations import (
    HubOperation, HubOperationError, HubOperationInputField as Field, HubOperationResult,
    HubQuery, register_operation, register_query,
)
from app.services.hub_record_access import identifier, require_actor


def require_admin(service):
    user, _access = require_actor(service, "settings", "view")
    if user.role != "admin":
        raise HubOperationError("Nur Hub-Administratoren koennen PDF-Vorlagen und AGBs verwalten.")
    return user


def domain_service(service, family):
    return HubPdfTemplateService(db=service.db) if family == "pdf_templates" else HubLegalTermsService(db=service.db)


def validate(values, fields):
    if set(values) - {field.name for field in fields}:
        raise HubOperationError("Unbekannte Vorlageneingabe.")
    for field in fields:
        value = values.get(field.name, "")
        if not isinstance(value, str) or len(value) > (field.max_length or 255):
            raise HubOperationError(f"{field.label}: ungueltiger oder zu langer Wert.")
        if field.required and not value.strip():
            raise HubOperationError(f"{field.label} fehlt.")
        if field.name in values and field.options and value not in dict(field.options):
            raise HubOperationError(f"{field.label}: ungueltige Auswahl.")


def get_record(service, values, family, *, lock=False):
    model = HubPdfTemplate if family == "pdf_templates" else HubLegalTerms
    key = "template_id" if family == "pdf_templates" else "legal_terms_id"
    record_id = identifier(values.get(key, ""))
    statement = select(model).where(model.id == record_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    record = service.db.scalar(statement)
    if record is None or getattr(record, "is_archived", False):
        raise HubOperationError("Die Vorlage wurde nicht gefunden.")
    expected = values.get("expected_version", "")
    if expected and (not expected.isdecimal() or int(expected) != record.version):
        raise HubOperationError("Die Vorlage wurde inzwischen geaendert. Bitte erneut lesen.")
    return record


def mutate(service, values, *, family, action):
    actor = require_admin(service)
    fields = pdf_fields(action) if family == "pdf_templates" else legal_fields(action)
    validate(values, fields)
    domain = domain_service(service, family)
    record = None if action == "create" else get_record(service, values, family, lock=True)
    try:
        if family == "legal_terms":
            if action == "create":
                record = domain.create(actor=actor, name=values["name"])
            elif action == "update":
                record = domain.update(actor=actor, legal_terms_id=record.id,
                    name=values.get("name", record.name), content_html=values.get("content_html", record.content_html))
            elif action == "duplicate":
                record = domain.duplicate(actor=actor, legal_terms_id=record.id)
            elif action == "delete":
                domain.delete(actor=actor, legal_terms_id=record.id)
        elif action == "create":
            record = domain.create(actor=actor, document_type=values["document_type"], name=values["name"])
        elif action == "rename":
            record = domain.rename(actor=actor, template_id=record.id, name=values["name"])
        elif action == "update_block":
            block = next(item for item in domain.editor_view(record).blocks if item.key == values["block_key"])
            record = domain.update_block(actor=actor, template_id=record.id, block_key=block.key,
                content_html=values.get("content_html", block.content_html),
                is_visible=values.get("is_visible", str(block.is_visible).lower()) == "true")
        elif action == "set_legal_terms":
            if "legal_terms_id" not in values:
                raise HubOperationError("AGB-ID angeben oder ausdruecklich leer lassen, um die Zuordnung zu entfernen.")
            record = domain.set_legal_terms(actor=actor, template_id=record.id, legal_terms_id=identifier(values["legal_terms_id"]))
        elif action == "update_positions":
            columns = json.loads(values["columns"])
            if not isinstance(columns, list) or any(not isinstance(item, dict) or not isinstance(item.get("is_enabled"), bool) for item in columns):
                raise HubOperationError("columns muss eine JSON-Liste mit booleschem is_enabled je Spalte sein.")
            record = domain.update_positions(actor=actor, template_id=record.id, columns=columns,
                show_totals=values["show_totals"] == "true" if "show_totals" in values else None)
        elif action == "duplicate":
            record = domain.duplicate(actor=actor, template_id=record.id)
        elif action == "set_default":
            record = domain.set_default(actor=actor, template_id=record.id)
        elif action == "delete":
            domain.delete(actor=actor, template_id=record.id)
    except ValueError as exc:
        raise HubOperationError(str(exc)) from exc
    key = "template_id" if family == "pdf_templates" else "legal_terms_id"
    anchor = "account-pdf-templates" if family == "pdf_templates" else "account-legal-terms"
    query_key = "pdf_template" if family == "pdf_templates" else "legal_terms"
    return HubOperationResult("Vorlagenverwaltung oeffnen", f"/settings?{query_key}={record.id}#{anchor}", record.id,
        outputs={key: str(record.id), "version": str(record.version), "document_type": getattr(record, "document_type", "")})


def offset(values, key="offset"):
    raw = values.get(key) or "0"
    if not raw.isdecimal() or len(raw) > 7:
        raise HubOperationError("Ungueltiger Lese-Offset.")
    return int(raw)


def metadata(record, family):
    return {"template_id" if family == "pdf_templates" else "legal_terms_id": str(record.id),
        "name": record.name, "version": str(record.version),
        **({"document_type": record.document_type, "is_default": record.is_default,
            "legal_terms_id": str(record.legal_terms_id or "")} if family == "pdf_templates" else {})}


def list_records(service, values, *, family):
    require_admin(service)
    domain = domain_service(service, family)
    records = domain.list_templates(document_type=values.get("document_type") or None, initialize=False) if family == "pdf_templates" else domain.list_terms(initialize=False)
    query = values.get("query", "").casefold()
    records = [record for record in records if query in record.name.casefold()]
    start = offset(values)
    return {"items": [metadata(record, family) for record in records[start:start + 25]], "total": len(records),
        "next_offset": str(start + 25) if len(records) > start + 25 else ""}


def chunk(text, start):
    return {"content_html": text[start:start + 6000], "content_truncated": start > 0 or len(text) > 6000,
        "next_text_offset": str(start + 6000) if len(text) > start + 6000 else ""}


def read_record(service, values, *, family):
    require_admin(service)
    record = get_record(service, values, family)
    result = metadata(record, family)
    start = offset(values, "text_offset")
    if family == "legal_terms":
        return {**result, **chunk(record.content_html, start)}
    view = domain_service(service, family).editor_view(record)
    if values.get("block_key"):
        block = next((item for item in view.blocks if item.key == values["block_key"]), None)
        if block is None:
            raise HubOperationError("Der Vorlagenbereich wurde nicht gefunden.")
        return {**result, "block_key": block.key, "is_visible": block.is_visible, **chunk(block.content_html, start)}
    if start:
        raise HubOperationError("Fuer Textfortsetzungen einen block_key angeben.")
    return {**result,
        "blocks": [{"block_key": item.key, "label": item.label, "is_visible": item.is_visible,
            "content_html": item.content_html[:1000], "content_truncated": len(item.content_html) > 1000,
            "next_text_offset": "1000" if len(item.content_html) > 1000 else ""} for item in view.blocks],
        "show_totals": view.show_totals,
        "columns": [{"key": item.key, "label": item.label, "source_key": item.source_key, "width": item.width,
            "alignment": item.alignment, "is_enabled": item.is_enabled} for item in view.columns],
        "column_sources": [{"key": key, "label": label} for key, label, _sample in PDF_LINE_SOURCES]}


def placeholders(service, values):
    require_admin(service)
    view = HubPdfTemplateService(db=service.db).editor_view(get_record(service, values, "pdf_templates"))
    start = offset(values)
    return {"items": [{"token": item.token, "label": item.label} for item in view.placeholders[start:start + 25]],
        "next_offset": str(start + 25) if len(view.placeholders) > start + 25 else ""}


ACTION_LABELS = {
    "create": "anlegen", "rename": "umbenennen", "update_block": "Textbereich bearbeiten",
    "set_legal_terms": "AGB zuordnen", "update_positions": "Positionstabelle bearbeiten",
    "duplicate": "duplizieren", "set_default": "als Standard festlegen", "delete": "loeschen",
    "update": "bearbeiten",
}


for family, actions, fields in (
    ("pdf_templates", ("create", "rename", "update_block", "set_legal_terms", "update_positions", "duplicate", "set_default", "delete"), pdf_fields),
    ("legal_terms", ("create", "update", "duplicate", "delete"), legal_fields),
):
    for action in actions:
        label = "PDF-Vorlage" if family == "pdf_templates" else "Rechtstext"
        register_operation(HubOperation(key=f"finance.{family}.{action}", module="settings", label=f"{label}: {ACTION_LABELS[action]}",
            description="Globale PDF-Vorlagen/AGB-Verwaltung wie in Einstellungen, nur fuer Hub-Administratoren. Aendert keine bereits erzeugte PDF.",
            input_guide="Vorher lesen und expected_version uebergeben. Fehlende Update-Felder behalten ihren Wert. Gekuerzten HTML-Text vor Ersetzung vollstaendig nachladen. Spaltenliste vollstaendig uebernehmen; Breiten aktivierter Spalten ergeben 100. AGB-Aenderungen versionieren auch verknuepfte Vorlagen; Loeschschutz bleibt aktiv. Neuanlage nutzt den Masken-Standardinhalt, keinen erfundenen Rechtstext.",
            input_fields=partial(fields, action), preview_fields=tuple((field.name, field.label) for field in fields(action)),
            execute=partial(mutate, family=family, action=action),
            result_fields=(("template_id" if family == "pdf_templates" else "legal_terms_id", "Datensatz-ID"), ("version", "Neue Version"), ("document_type", "Belegart"))))
    filter_fields = (Field("query", "Namenssuche"), Field("offset", "Seitenbeginn"))
    if family == "pdf_templates":
        filter_fields += (Field("document_type", "Belegart", options=tuple((item.key, item.label) for item in PDF_TEMPLATE_TYPES)),)
    register_query(HubQuery(f"finance.{family}.list", "Vorlagen lesen, keine Standardanlage oder externe Abfrage. 25 Treffer pro Seite, nur Admin.", filter_fields, partial(list_records, family=family)))
    target = Field("template_id" if family == "pdf_templates" else "legal_terms_id", "Datensatz-ID", required=True, max_length=18)
    read_fields = (target, Field("text_offset", "Textbeginn")) + ((Field("block_key", "Einzelner HTML-Bereich"),) if family == "pdf_templates" else ())
    register_query(HubQuery(f"finance.{family}.read", "Aktuelle Version, Inhalt und Einstellungen aus demselben Fachservice wie die Maske. Lange Texte per text_offset weiterlesen; bei PDFs block_key angeben.", read_fields, partial(read_record, family=family)))
register_query(HubQuery("finance.pdf_templates.placeholders", "Zulaessige Platzhalter wie im PDF-Editor, 25 pro Seite.",
    (Field("template_id", "Vorlagen-ID", required=True, max_length=18), Field("offset", "Seitenbeginn")), placeholders))
