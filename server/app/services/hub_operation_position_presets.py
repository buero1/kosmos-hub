"""Shared Finance position library, without agent-specific data copies."""

import json

from app.services.hub_finance_position_presets import HubFinancePositionPresetService, HubFinancePositionPresetError, POSITION_PRESET_LIBRARIES
from app.services.hub_operations import HubOperation, HubOperationError, HubOperationInputField as Field, HubOperationResult, HubQuery, register_operation, register_query
from app.services.hub_record_access import identifier, require_actor


def preset_service(service):
    return HubFinancePositionPresetService(db=service.db, cipher=service.cipher)


def get_preset(service, preset_id, library):
    require_actor(service, "finance", "view")
    try:
        return preset_service(service).get(preset_id=preset_id, library_key=library)
    except HubFinancePositionPresetError as exc:
        raise HubOperationError(str(exc)) from exc


def create(service, values):
    require_actor(service, "finance", "create")
    try:
        lines = json.loads(values.get("lines_json", ""))
    except ValueError as exc:
        raise HubOperationError("Die Positionen konnten nicht gelesen werden.") from exc
    preset = preset_service(service).create(library_key=values.get("library_key", ""), name=values.get("name", ""), lines=lines, actor_username=service.actor)
    return HubOperationResult("Positionsvorlagen oeffnen", "/finance/invoices" if preset.library_key == "invoices" else "/finance/offers", preset.id,
        outputs={"library_key": preset.library_key, "line_count": str(preset.line_count)})


def delete(service, values):
    require_actor(service, "finance", "delete")
    preset = preset_service(service).delete(preset_id=identifier(values.get("preset_id", "")), library_key=values.get("library_key", ""))
    return HubOperationResult("Positionsvorlagen oeffnen", "/finance/invoices" if preset.library_key == "invoices" else "/finance/offers", preset.id)


def list_presets(service, values):
    require_actor(service, "finance", "view")
    try:
        entries = preset_service(service).list_presets(library_key=values["library_key"])
    except HubFinancePositionPresetError as exc:
        raise HubOperationError(str(exc)) from exc
    query = values.get("query", "").casefold()
    entries = [entry for entry in entries if query in entry.name.casefold()]
    raw = values.get("offset") or "0"
    if not raw.isdecimal() or len(raw) > 7:
        raise HubOperationError("Ungueltiger Seitenbeginn.")
    start = int(raw)
    return {"items": [{"preset_id": str(entry.id), "name": entry.name, "line_count": entry.line_count} for entry in entries[start:start + 25]], "total": len(entries), "next_offset": str(start + 25) if len(entries) > start + 25 else ""}


def read_preset(service, values):
    preset = get_preset(service, identifier(values["preset_id"]), values["library_key"])
    raw = values.get("line_offset") or "0"
    if not raw.isdecimal() or len(raw) > 7:
        raise HubOperationError("Ungueltiger Positionsbeginn.")
    start = int(raw)
    if values.get("field"):
        field = values["field"]
        if start >= len(preset.lines) or field not in preset.lines[start]:
            raise HubOperationError("Das Positionsfeld ist nicht verfuegbar.")
        raw_offset = values.get("text_offset") or "0"
        if not raw_offset.isdecimal() or len(raw_offset) > 7:
            raise HubOperationError("Ungueltiger Textbeginn.")
        offset = int(raw_offset)
        text = preset.lines[start][field]
        return {"index": start, "field": field, "value": text[offset:offset + 6000], "next_text_offset": str(offset + 6000) if len(text) > offset + 6000 else ""}
    return {"preset_id": str(preset.id), "name": preset.name, "library_key": preset.library_key, "line_count": preset.line_count,
        "lines": [{"index": index, "values": {key: value[:500] for key, value in line.items()}, "truncated_fields": [key for key, value in line.items() if len(value) > 500]} for index, line in enumerate(preset.lines[start:start + 5], start)],
        "next_line_offset": str(start + 5) if len(preset.lines) > start + 5 else "",
        "usage": "Positionen fuer offers in offer_line__INDEX__FELD, fuer orders/invoices in document_line__INDEX__FELD uebernehmen. sales fuer Angebote/Auftraege, invoices fuer Rechnungen."}


_LIBRARY = Field("library_key", "Positionsbibliothek", required=True, options=tuple(POSITION_PRESET_LIBRARIES.items()))
register_operation(HubOperation(
    key="finance.presets.create", module="finance", label="Positionsvorlage speichern", description="Speichert ein benanntes Positionspaket wie in der Finance-Maske.",
    input_guide="library_key, name und lines_json (JSON-Liste mit name, article_id optional, sku, description, quantity, unit optional, unit_price, discount_percent optional, tax_rate pro Zeile). Kein Beleg wird erstellt.",
    preview_fields=(("library_key", "Bibliothek"), ("name", "Name"), ("lines_json", "Positionen")), execute=create,
    input_fields=lambda: (_LIBRARY, Field("name", "Name", required=True, max_length=255), Field("lines_json", "Positionen", required=True, max_length=500_000, encoding="JSON array of line objects")),
    result_fields=(("library_key", "Bibliothek"), ("line_count", "Anzahl Positionen")),
))
register_operation(HubOperation(
    key="finance.presets.delete", module="finance", label="Positionsvorlage loeschen", description="Loescht ein gespeichertes Positionspaket; bestehende Belegpositionen bleiben unveraendert.",
    input_guide="preset_id und library_key.", preview_fields=(("preset_id", "Positionspaket-ID"), ("library_key", "Bibliothek")), execute=delete,
    input_fields=lambda: (_LIBRARY, Field("preset_id", "Positionspaket-ID", required=True)),
))
register_query(HubQuery("finance.presets.list", "Positionspakete der Finance-Masken suchen.", (_LIBRARY, Field("query", "Namenssuche"), Field("offset", "Seitenbeginn")), list_presets))
register_query(HubQuery("finance.presets.read", "Gespeicherte Positionen wie in der Finance-Maske lesen, 5 pro Seite. truncated_fields vor Wiederverwendung mit line_offset (Zeilenindex), field und text_offset vollstaendig nachladen.", (_LIBRARY, Field("preset_id", "Positionspaket-ID", required=True), Field("line_offset", "Positionsbeginn"), Field("field", "Einzelnes langes Positionsfeld"), Field("text_offset", "Textbeginn")), read_preset))
