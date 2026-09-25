"""Discoverable Finance CRUD, reading and PDF operations shared with the web UI."""

from __future__ import annotations

from functools import partial

from sqlalchemy import select

from app.models.hub_pdf_template import HubPdfTemplate

from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import FINANCE_DOCUMENT_MODULES, HubFinanceDocumentService
from app.services.hub_finance_field_catalog import FINANCE_POSITION_UNITS
from app.services.hub_finance_operations_shared import (
    FINANCE_KINDS, PDF_KINDS, fields_for, finance_detail, finance_entries,
    finance_options, form_defaults, merge_form, prefix_for, require_record, stored_values,
)
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_finance_pdf_readers import load_pdf
from app.services.hub_operation_offers import _party_id, _offer_recipient
from app.services.hub_operations import (
    HubArtifact, HubOperation, HubOperationError, HubOperationInputField as Field,
    HubOperationResult, HubQuery,
    register_artifact, register_operation, register_query,
)
from app.services.hub_record_access import identifier, require_actor


def mutate(service, values, *, kind, action):
    user, access = require_actor(service, "finance", {"update": "edit"}.get(action, action))
    record = None if action == "create" else require_record(service, kind, identifier(values.get("record_id", "")), "edit" if action == "update" else action)
    domain = HubFinanceService(db=service.db, cipher=service.cipher) if kind in {"articles", "offers"} else HubFinanceDocumentService(db=service.db, cipher=service.cipher)
    if action == "delete":
        if kind == "articles":
            domain.delete_article(article_id=record.id)
        elif kind == "offers":
            domain.delete_offer(offer_id=record.id, after_commit=service.after_commit)
        else:
            domain.delete_document(module=FINANCE_DOCUMENT_MODULES[kind], document_id=record.id, after_commit=service.after_commit)
        return HubOperationResult("Liste oeffnen", f"/finance/{kind}", record.id)
    submitted = merge_form(service, kind, values, record)
    if kind == "articles":
        record = domain.create_article(submitted_values=submitted) if record is None else domain.update_article(article_id=record.id, submitted_values=submitted)
    else:
        parties = {}
        for party in (("customer", "lead") if kind == "offers" else ("customer",)):
            explicit = f"{party}_id" in values or f"{party}_name" in values
            parties[f"{party}_id"] = _party_id(service, values=values, kind=party, user=user, access=access) if explicit or record is None else getattr(record, f"{party}_id")
        contact_id = identifier(values["contact_id"]) if "contact_id" in values else getattr(record, "contact_id", None)
        template_id = identifier(values["pdf_template_id"]) if "pdf_template_id" in values else getattr(record, "pdf_template_id", None)
        kwargs = dict(**parties, contact_id=contact_id, pdf_template_id=template_id, submitted_values=submitted)
        if kind == "offers":
            record = domain.create_offer(**kwargs) if record is None else domain.update_offer(offer_id=record.id, **kwargs)
        else:
            module = FINANCE_DOCUMENT_MODULES[kind]
            previous_link = getattr(record, f"{module.link_attribute}_id", None) if module.link_attribute and record else None
            link_id = identifier(values["linked_record_id"]) if "linked_record_id" in values else previous_link
            if link_id and module.link_attribute:
                linked_kind = {"offer": "offers", "order": "orders", "invoice": "invoices"}[module.link_attribute]
                require_record(service, linked_kind, link_id)
            kwargs.update(module=module, link_id=link_id)
            record = domain.create_document(**kwargs) if record is None else domain.update_document(document_id=record.id, **kwargs)
    return record_result(service, kind, record)


def record_result(service, kind, record):
    token = HubFinancePdfService(db=service.db, cipher=service.cipher).queue(document_type=kind, document_id=record.id) if kind in PDF_KINDS else ""
    outputs = {"customer_id": str(getattr(record, "customer_id", None) or ""), "lead_id": str(getattr(record, "lead_id", None) or "")}
    if kind in PDF_KINDS:
        email, name = _offer_recipient(service, record)
        outputs.update(artifact_ref=artifact_reference(kind, record.id), recipient_email=email, recipient_name=name)
    return HubOperationResult("Datensatz oeffnen", f"/finance/{kind}/{record.id}", record.id, background_token=token, outputs=outputs)


def artifact_reference(kind, record_id):
    return f"finance.offer.pdf:{record_id}" if kind == "offers" else f"finance.document.pdf:{kind}/{record_id}"


def load_document_pdf(service, reference):
    kind, separator, raw_id = reference.partition("/")
    if not separator or kind not in PDF_KINDS:
        raise HubOperationError("Dieser PDF-Anhang ist nicht verfuegbar.")
    record = require_record(service, kind, identifier(raw_id))
    pdf, content = load_pdf(service, kind, record.id, source="available", wait=True)
    email, _ = _offer_recipient(service, record)
    return HubArtifact(pdf.filename or f"{kind}-{record.id}.pdf", "application/pdf", content, email)


def generate_pdf(service, values):
    kind = values.get("kind", "")
    if kind not in PDF_KINDS:
        raise HubOperationError("Fuer diese Belegart wird keine PDF erzeugt.")
    record = require_record(service, kind, identifier(values.get("record_id", "")), "edit")
    return record_result(service, kind, record)


def input_fields(kind, action):
    result = [] if action == "create" else [Field("record_id", "Datensatz-ID", required=True)]
    if action == "delete":
        return tuple(result)
    prefix = prefix_for(kind)
    module = FINANCE_DOCUMENT_MODULES.get(kind)
    for field in fields_for(kind):
        if field.read_only:
            continue
        if field.key in {"customer", "contact"}:
            result.append(Field(f"{field.key}_id", field.label, required=field.required and action == "create" and kind != "offers", context_type="customer" if field.key == "customer" else ""))
        elif module and field.key == module.link_key:
            result.append(Field("linked_record_id", field.label, required=field.required and action == "create"))
        elif kind == "recurring-invoices" and field.key in {"custom_interval", "payment_due"}:
            units = ("day", "week", "month", "year") if field.key == "custom_interval" else ("day", "week", "year")
            result.extend((Field(f"{prefix}_field__{field.key}_count", field.label + ": Anzahl"), Field(f"{prefix}_field__{field.key}_unit", field.label + ": Zeiteinheit", options=tuple((unit, unit) for unit in units))))
        else:
            result.append(Field(f"{prefix}_field__{field.key}", field.label, required=field.required and action == "create" and not (kind == "recurring-invoices" and field.key == "next_invoice_date"), options=field.options,
                                encoding="HTML" if field.display_type == "HTML" else ""))
    if kind != "articles":
        result.extend((Field("pdf_template_id", "PDF-Vorlage (leer: Standard)"), Field("lines_mode", "Positionen: patch erhaelt nicht angegebene Zeilen, replace ersetzt alle", options=(("patch", "Teilweise aendern"), ("replace", "Alle ersetzen")))))
    if kind == "offers":
        result.append(Field("lead_id", "Lead statt Kunde", context_type="lead"))
    return tuple(result)


def input_guide(kind, action):
    if action == "delete":
        return "record_id. Loescht den Datensatz im Hub einschliesslich seiner Positionen und PDFs, nicht in Zoho."
    prefix = prefix_for(kind)
    guide = "Nur angegebene Felder werden geaendert; zum Leeren explizit leeren Text uebergeben. Datumswerte YYYY-MM-DD. " if action == "update" else "Standards und Pflichtfelder wie in der Maske. Datumswerte YYYY-MM-DD. "
    if kind == "articles":
        return guide
    guide += (
        f"Positionen: {prefix}_line__0__name (erforderlich), __article_id, __sku, __description, "
        "__quantity (Standard 1), __unit (optional), __unit_price (erforderlich), __discount_percent (Standard 0), "
        "__tax_rate (Standard 19; 0/7/19). Alle Schluessel mit gleichem Praefix und Index; weitere Indizes 1,2 usw. "
        f"Einheiten: {', '.join(FINANCE_POSITION_UNITS)}. __delete=true entfernt eine Zeile. "
        "Bestehende Zeilenindizes zuerst mit finance.read lesen. lines_mode=replace nur wenn alle Positionen ersetzt werden sollen. "
        "Keine Preise oder Leistungen erfinden. Ansprechpartner muss zum Kunden gehoeren. "
    )
    if kind == "offers":
        guide += "Genau ein Kunde oder Lead; bei Lead muss contact_id leer sein. Beim Wechsel der Zuordnung alte Gegenverknuepfung explizit leeren. "
    if kind == "recurring-invoices":
        guide += "custom_interval_count/unit nur bei interval_unit=custom erforderlich; payment_due_count/unit optional zusammen. Aktiv setzt den periodischen Rechnungsplan aktiv; keine E-Mail wird dadurch versendet. Pausiert/Beendet leert next_invoice_date und stoppt die Erstellung. Beim erneuten Aktivieren muss next_invoice_date explizit gesetzt werden."
    return guide


def preview(values, *, kind, action):
    labels = {field.name: field.label for field in input_fields(kind, action)}
    lines = [f"{labels.get(key, key)}: {value or '(leer)'}" for key, value in values.items()]
    if kind == "recurring-invoices" and action != "delete":
        lines.append("Periodischer Rechnungsplan: Status und naechstes Rechnungsdatum bestimmen die kuenftige Verarbeitung. Kein E-Mail-Versand.")
    return tuple(lines)


def _offset(values, key="offset"):
    raw = values.get(key) or "0"
    if not raw.isdecimal() or int(raw) > 1_000_000:
        raise HubOperationError("Ungueltiger Seitenbeginn.")
    return int(raw)


def read_list(service, values):
    kind = values["kind"]
    entries = finance_entries(service, kind)
    items = []
    for entry in entries:
        if kind == "articles":
            record, title = entry.article, entry.name
            extra = {"sku": entry.sku, "net_price": entry.net_price_form, "tax_rate": entry.tax_rate_value, "unit": entry.unit}
        elif kind == "offers":
            record, title = entry.offer, entry.offer_number
            extra = {"party": entry.linked_name, "total_gross": entry.total_gross, "date": entry.offer_date}
        else:
            record, title = entry.document, entry.identifier
            extra = {"party": entry.customer_name, "total_gross": entry.total_gross, "date": entry.document_date}
        item = {"record_id": str(record.id), "title": title, "status": entry.status, "href": f"/finance/{kind}/{record.id}", **extra}
        if kind != "articles":
            item.update(customer_id=str(record.customer_id or ""), lead_id=str(getattr(record, "lead_id", None) or ""))
        if values.get("customer_id") and item.get("customer_id") != values["customer_id"]:
            continue
        if values.get("lead_id") and item.get("lead_id") != values["lead_id"]:
            continue
        if values.get("query", "").casefold() not in " ".join(str(value) for value in item.values()).casefold():
            continue
        items.append(item)
    offset = _offset(values)
    return {"items": items[offset:offset + 25], "total": len(items), "next_offset": str(offset + 25) if len(items) > offset + 25 else ""}


def read_detail(service, values):
    kind = values["kind"]
    record = require_record(service, kind, identifier(values["record_id"]))
    detail = finance_detail(service, kind, record.id)
    prefix = prefix_for(kind)
    raw = stored_values(service, kind, record)
    if values.get("field"):
        key = values["field"]
        if key not in raw:
            raise HubOperationError("Dieses Finance-Feld ist nicht verfuegbar.")
        offset = _offset(values, "text_offset")
        return {"field": key, "value": raw[key][offset:offset + 6000], "total_length": len(raw[key]), "next_text_offset": str(offset + 6000) if len(raw[key]) > offset + 6000 else ""}
    truncated = [key for key, value in raw.items() if len(value) > 500]
    raw = {key: value[:500] for key, value in raw.items()}
    writable = {field.name for field in input_fields(kind, "update")}
    fields = {field.key: field.value[:500] for field in detail.fields}
    result = {"record_id": str(record.id), "kind": kind, "fields": fields,
              "input_values": {key: value for key, value in raw.items() if key in writable},
              "truncated_fields": truncated, "text_note": "Gekuerzte Felder vor Wiederverwendung mit field und text_offset vollstaendig nachladen." if truncated else "",
              "href": f"/finance/{kind}/{record.id}"}
    if kind != "articles":
        result["input_values"].update(customer_id=str(record.customer_id or ""), contact_id=str(record.contact_id or ""), pdf_template_id=str(record.pdf_template_id or ""))
        if kind == "offers":
            result["input_values"]["lead_id"] = str(record.lead_id or "")
        elif FINANCE_DOCUMENT_MODULES[kind].link_attribute:
            result["input_values"]["linked_record_id"] = str(getattr(record, FINANCE_DOCUMENT_MODULES[kind].link_attribute + "_id") or "")
        offset = _offset(values, "line_offset")
        result["lines"] = [{"index": index, "input_values": {key: value for key, value in raw.items() if key.startswith(f"{prefix}_line__{index}__")}}
                           for index in range(offset, min(len(record.lines), offset + 20))]
        result.update(line_count=len(record.lines), next_line_offset=str(offset + 20) if len(record.lines) > offset + 20 else "", total_gross=str(detail.totals.total_gross))
    if kind in PDF_KINDS:
        state = HubFinancePdfService(db=service.db, cipher=service.cipher).view(document_type=kind, document_id=record.id)
        imported_pdf = getattr(detail, "invoice_pdf", None) or getattr(detail, "order_pdf", None)
        result.update(artifact_ref=artifact_reference(kind, record.id), pdf_status=state.status if state else ("ready" if imported_pdf else "missing"))
        email, name = _offer_recipient(service, record)
        result.update(recipient_email=email, recipient_name=name)
    return result


def read_options(service, values):
    kind, category = values["kind"], values["category"]
    if category == "pdf_templates":
        require_actor(service, "finance", "view")
        template_type = "invoices" if kind == "recurring-invoices" else kind
        # Unlike the form's template initializer, discovery must never write defaults.
        templates = service.db.scalars(select(HubPdfTemplate).where(HubPdfTemplate.document_type == template_type).order_by(HubPdfTemplate.is_default.desc(), HubPdfTemplate.name, HubPdfTemplate.id)) if template_type in PDF_KINDS else ()
        entries = [{"id": str(item.id), "name": item.name, "is_default": item.is_default} for item in templates]
    else:
        options = finance_options(service, kind, record_id=identifier(values.get("record_id", "")))
        entries = []
        for item in options[category]:
            if values.get("customer_id") and str(getattr(item, "customer_id", item.id if category == "customers" else "")) != values["customer_id"]:
                continue
            entries.append({"id": str(item.id), "name": getattr(item, "name", None) or getattr(item, "label", ""),
                            **({"customer_id": str(item.customer_id)} if hasattr(item, "customer_id") else {})})
    query = values.get("query", "").casefold()
    entries = [item for item in entries if query in item["name"].casefold()]
    offset = _offset(values)
    return {"items": entries[offset:offset + 25], "total": len(entries), "next_offset": str(offset + 25) if len(entries) > offset + 25 else ""}


def dunning_source(service, values):
    invoice = require_record(service, "invoices", identifier(values["record_id"]))
    draft = HubFinanceDocumentService(db=service.db, cipher=service.cipher).dunning_draft_from_invoice(invoice_id=invoice.id)
    return {"input_values": {**draft.submitted_values, "customer_id": str(draft.customer_id or ""), "contact_id": str(draft.contact_id or ""), "linked_record_id": str(invoice.id)}}


_LABELS = {"articles": "Artikel", "offers": "Angebot", **{key: module.singular for key, module in FINANCE_DOCUMENT_MODULES.items()}}
_RESULTS = (("artifact_ref", "PDF-Verweis fuer attachment_ref eines E-Mail-Entwurfs"), ("recipient_email", "E-Mail des Ansprechpartners"), ("recipient_name", "Ansprechpartner"), ("customer_id", "Kunden-ID"), ("lead_id", "Lead-ID"))
for _kind in FINANCE_KINDS:
    for _action, _label in (("create", "anlegen"), ("update", "bearbeiten"), ("delete", "loeschen")):
        if (_kind, _action) == ("offers", "create"):
            continue
        register_operation(HubOperation(
            key=f"finance.{_kind}.{_action}", module="finance", label=f"{_LABELS[_kind]} {_label}",
            description=f"{_LABELS[_kind]} im Hub {_label}, mit denselben Pruefungen wie die Maske.",
            input_guide=input_guide(_kind, _action), preview_fields=(),
            execute=partial(mutate, kind=_kind, action=_action),
            defaults=partial(form_defaults, _kind) if _action == "create" else None,
            input_fields=partial(input_fields, _kind, _action), preview_builder=partial(preview, kind=_kind, action=_action),
            result_fields=_RESULTS if _kind in PDF_KINDS and _action != "delete" else (),
        ))
register_operation(HubOperation(
    key="finance.pdf.generate", module="finance", label="Beleg-PDF erzeugen", description="Erzeugt die PDF eines vorhandenen Finance-Belegs, ohne E-Mail-Versand.",
    input_guide="kind und record_id. Ergebnis artifact_ref kann nach PDF-Erzeugung an einen E-Mail-Entwurf angehaengt werden.",
    preview_fields=(("kind", "Belegart"), ("record_id", "Beleg-ID")), execute=generate_pdf,
    input_fields=lambda: (Field("kind", "Belegart", required=True, options=tuple((kind, _LABELS[kind]) for kind in PDF_KINDS)), Field("record_id", "Beleg-ID", required=True)), result_fields=_RESULTS,
))
register_artifact("finance.document.pdf", load_document_pdf)
_KIND_FIELD = Field("kind", "Finance-Modul", required=True, options=tuple((kind, _LABELS[kind]) for kind in FINANCE_KINDS))
register_query(HubQuery("finance.list", "Finance-Datensaetze suchen, nur sichtbare Kunden/Leads; 25 Treffer pro Seite.", (_KIND_FIELD, Field("query", "Suche"), Field("customer_id", "Kunde"), Field("lead_id", "Lead"), Field("offset", "Seitenbeginn, Standard 0")), read_list))
register_query(HubQuery("finance.read", "Finance-Details und Eingabewerte lesen, inklusive 20 Positionen pro Seite, Summen, Beziehungen und PDF-Verweis. Lange Texte sind markiert; mit field (voller Eingabeschluessel) und text_offset separat nachladen.", (_KIND_FIELD, Field("record_id", "Datensatz-ID", required=True), Field("line_offset", "Positionsbeginn, Standard 0"), Field("field", "Einzelner Eingabeschluessel fuer langen Text"), Field("text_offset", "Textbeginn, Standard 0")), read_detail))
register_query(HubQuery("finance.options", "Zulaessige Verknuepfungen und PDF-Vorlagen der Finance-Maske suchen; record_id behaelt bestehende berechtigte Verknuepfungen beim Bearbeiten. Artikel ueber finance.list suchen.", (_KIND_FIELD, Field("category", "Auswahlliste", required=True, options=tuple((key, key) for key in ("customers", "leads", "contacts", "link_options", "pdf_templates"))), Field("record_id", "Optional: vorhandener Finance-Datensatz beim Bearbeiten"), Field("customer_id", "Nach Kunde filtern"), Field("query", "Namenssuche"), Field("offset", "Seitenbeginn")), read_options))
register_query(HubQuery("finance.dunning_source", "Liest die Vorbelegung fuer eine Mahnung aus einer Rechnung, wie in der Maske; erstellt noch keinen Datensatz.", (Field("record_id", "Rechnungs-ID", required=True),), dunning_source))
