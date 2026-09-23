"""Bounded relational selection over authorized Hub list projections, not SQL."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from time import monotonic

from app.services.hub_operations import HubOperationError

MAX_ROWS = 5000
MAX_PAGES = 250
MAX_SOURCE_CHARS = 4_000_000
MAX_SECONDS = 30
KINDS = ("text", "number", "version", "date")
OPERATORS = ("eq", "ne", "contains", "in", "lt", "lte", "gt", "gte", "is_empty", "not_empty")


def selection_schema():
    name = {"type": "string", "maxLength": 80}
    typed_field = {"field": name, "type": {"type": "string", "enum": list(KINDS)}}
    return {"type": "object", "additionalProperties": False, "properties": {
        "schema_only": {"type": "boolean", "description": "Read only the first page's column names/types, no record values."},
        "fields": {"type": "array", "items": name, "minItems": 1, "maxItems": 20},
        "filters": {"type": "array", "maxItems": 10, "items": {"type": "object", "additionalProperties": False,
            "properties": {**typed_field, "op": {"type": "string", "enum": list(OPERATORS)},
                "value": {"description": "Scalar comparison value, or array for in.",
                    "anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"},
                              {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}, "maxItems": 50}]}},
            "required": ["field", "op"]}},
        "order_by": {"type": "array", "maxItems": 3, "items": {"type": "object", "additionalProperties": False,
            "properties": {**typed_field, "direction": {"type": "string", "enum": ["asc", "desc"]}}, "required": ["field"]}},
        "extreme": {"type": "object", "additionalProperties": False,
            "properties": {**typed_field, "kind": {"type": "string", "enum": ["min", "max"]}}, "required": ["field", "kind"],
            "description": "Keep all ties at the global minimum/maximum AFTER filters, BEFORE result pagination."},
        "offset": {"type": "integer", "minimum": 0, "maximum": MAX_ROWS},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "count_only": {"type": "boolean"},
    }}


def _fail(message="Ungueltige Datenauswahl."):
    raise HubOperationError(message)


def _field(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", value):
        _fail("Nur vorhandene Spaltennamen sind erlaubt, keine Ausdruecke.")


def _typed_field(value, allowed):
    if not isinstance(value, dict) or set(value) - allowed:
        _fail()
    _field(value.get("field"))
    if value.get("type", "text") not in KINDS:
        _fail("Unbekannter Vergleichstyp.")


def _convert(value, kind):
    if value is None or value == "" or isinstance(value, (list, dict)):
        return None
    value = str(value).strip()
    if not value:
        return None
    if kind == "text":
        return value.casefold()
    if len(value) > 200:
        return None
    if kind == "number":
        try:
            number = Decimal(value)
            return number if number.is_finite() else None
        except InvalidOperation:
            return None
    if kind == "version":
        # Numeric release components, never lexicographic 6.10 < 6.9.
        if not re.fullmatch(r"[vV]?\d+(?:\.\d+){0,7}", value):
            return None
        parts = [int(part) for part in value.lstrip("vV").split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC)
    except ValueError:
        return None


def _validate(selection):
    if not isinstance(selection, dict) or set(selection) - selection_schema()["properties"].keys():
        _fail()
    for key in ("schema_only", "count_only"):
        if key in selection and type(selection[key]) is not bool:
            _fail()
    if selection.get("schema_only") and set(selection) != {"schema_only"}:
        _fail("schema_only nicht mit einer Datenauswahl kombinieren.")
    for key, default, low, high in (("offset", 0, 0, MAX_ROWS), ("limit", 25, 1, 100)):
        value = selection.get(key, default)
        if type(value) is not int or not low <= value <= high:
            _fail("Ungueltige Ergebnis-Seitengroesse.")
    fields = selection.get("fields")
    if fields is not None:
        if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
            _fail()
        for name in fields:
            _field(name)
        if len(set(fields)) != len(fields):
            _fail()
    for key, maximum in (("filters", 10), ("order_by", 3)):
        values = selection.get(key, [])
        if not isinstance(values, list) or len(values) > maximum:
            _fail()
        for item in values:
            _typed_field(item, {"field", "type", "op", "value"} if key == "filters" else {"field", "type", "direction"})
            if key == "order_by":
                if item.get("direction", "asc") not in ("asc", "desc"):
                    _fail()
                continue
            op = item.get("op")
            if op not in OPERATORS or (op not in ("is_empty", "not_empty") and "value" not in item):
                _fail()
            if op in ("is_empty", "not_empty"):
                continue
            value = item["value"]
            candidates = value if op == "in" else [value]
            if not isinstance(candidates, list) or not 1 <= len(candidates) <= 50:
                _fail()
            for candidate in candidates:
                if candidate is not None and not isinstance(candidate, (str, int, float, bool)):
                    _fail()
                if candidate is not None and len(str(candidate)) > 500:
                    _fail()
                if candidate not in (None, "") and _convert(candidate, item.get("type", "text")) is None:
                    _fail("Vergleichswert passt nicht zum gewaehlten Typ.")
            if op == "contains" and item.get("type", "text") != "text":
                _fail()
    if "extreme" in selection:
        _typed_field(selection["extreme"], {"field", "type", "kind"})
        if selection["extreme"].get("kind") not in ("min", "max"):
            _fail()


def _matches(row, condition):
    raw = row.get(condition["field"])
    op = condition["op"]
    if op in ("is_empty", "not_empty"):
        empty = raw is None or (isinstance(raw, str) and not raw.strip())
        return empty if op == "is_empty" else not empty
    kind = condition.get("type", "text")
    current = _convert(raw, kind)
    if current is None and raw is not None and str(raw).strip():
        return False
    value = condition["value"]
    if op == "in":
        return current in [_convert(item, kind) for item in value]
    target = _convert(value, kind)
    if op == "eq":
        return current == target
    if op == "ne":
        return current != target
    if current is None or target is None:
        return False
    if op == "contains":
        return target in current
    return {"lt": current < target, "lte": current <= target, "gt": current > target, "gte": current >= target}[op]


def select_query(service, key, values, selection, *, definition):
    """No cached authorization: each source page goes through the same gateway."""
    _validate(selection)
    if "offset" not in {field.name for field in definition.input_fields}:
        _fail("Diese Abfrage ist keine durchblaetterbare Liste; normalen Lesezugriff verwenden.")
    if values.get("offset", "") not in ("", "0"):
        _fail("Quell-Liste bei offset 0 beginnen; Ergebnis-Offset in select setzen.")
    rows, metadata = [], {}
    source_chars = 0
    cursor = "0"
    started = monotonic()
    expected_total = None
    for page_index in range(MAX_PAGES):
        if monotonic() - started > MAX_SECONDS:
            _fail("Lesebudget erreicht. Keine vollstaendige Auswertung moeglich; Quellfilter eingrenzen.")
        page = service.query(key, {**values, "offset": cursor})
        if not isinstance(page, dict) or not isinstance(page.get("items"), list) or "next_offset" not in page:
            _fail("Diese Abfrage liefert keine vollstaendig durchblaetterbare Datensatzliste.")
        items = page["items"]
        if any(not isinstance(row, dict) for row in items):
            _fail("Ungeeignetes Listenformat.")
        if page_index == 0:
            metadata = {name: value for name, value in page.items() if name not in ("items", "next_offset", "offset", "total")}
            expected_total = page.get("total")
        if selection.get("schema_only"):
            columns = {name: "number" if type(value) in (int, float) else "text" if isinstance(value, str) else type(value).__name__
                for row in items for name, value in row.items()}
            return {"fields": columns, "schema_only": True, "sampled_rows": len(items), "source_metadata": metadata}
        if page.get("total") != expected_total:
            _fail("Der Listenbestand hat sich waehrend der Auswertung geaendert. Erneut lesen.")
        source_chars += len(json.dumps(page, ensure_ascii=False))
        rows.extend(items)
        if len(rows) > MAX_ROWS or source_chars > MAX_SOURCE_CHARS or monotonic() - started > MAX_SECONDS:
            _fail("Lesebudget erreicht. Keine vollstaendige Auswertung moeglich; Quellfilter eingrenzen.")
        next_offset = page["next_offset"]
        if next_offset is None or next_offset == "":
            break
        if not items or not str(next_offset).isdecimal() or len(str(next_offset)) > 7 or int(next_offset) <= int(cursor):
            _fail("Unvollstaendige oder wiederholte Quellseite; Auswertung abgebrochen.")
        cursor = str(next_offset)
    else:
        _fail("Zu viele Quellseiten; keine vollstaendige Auswertung. Quellfilter eingrenzen.")
    if expected_total is not None and (type(expected_total) is not int or expected_total != len(rows)):
        _fail("Die Quell-Liste ist unvollstaendig; keine globale Auswertung moeglich.")
    fields = selection.get("fields")
    comparisons = [*selection.get("filters", []), *selection.get("order_by", [])]
    if "extreme" in selection:
        comparisons.append(selection["extreme"])
    names = set(fields or []) | {item["field"] for item in comparisons}
    known = {name for row in rows for name in row}
    if rows and names - known:
        _fail("Unbekannte Spalte. Mit select.schema_only die vorhandenen Spalten nachsehen.")
    if comparisons and any(row.get("next_text_offset") for row in rows):
        _fail("Gekuerzte Listenwerte erlauben keine vollstaendige Auswertung. Detailtexte gezielt lesen.")
    invalid = {item["field"]: sum(_convert(row.get(item["field"]), item.get("type", "text")) is None for row in rows)
        for item in comparisons if item.get("type", "text") != "text"}
    selected = [row for row in rows if all(_matches(row, condition) for condition in selection.get("filters", []))]
    extreme_result = None
    if "extreme" in selection:
        choice = selection["extreme"]
        pairs = [(_convert(row.get(choice["field"]), choice.get("type", "text")), row) for row in selected]
        usable = [(value, row) for value, row in pairs if value is not None]
        if usable:
            extreme = (min if choice["kind"] == "min" else max)(value for value, row in usable)
            selected = [row for value, row in usable if value == extreme]
            extreme_result = {"field": choice["field"], "kind": choice["kind"], "value": selected[0][choice["field"]]}
        else:
            selected = []
    for order in reversed(selection.get("order_by", [])):
        def convert(row):
            return _convert(row.get(order["field"]), order.get("type", "text"))
        present = [row for row in selected if convert(row) is not None]
        missing = [row for row in selected if convert(row) is None]
        selected = sorted(present, key=convert, reverse=order.get("direction") == "desc") + missing
    offset, limit = selection.get("offset", 0), selection.get("limit", 25)
    result = [] if selection.get("count_only") else selected[offset:offset + limit]
    if fields:
        result = [{name: row.get(name) for name in fields} for row in result]
    return {"items": result, "total": len(selected), "source_total": len(rows), "source_pages": page_index + 1,
        "source_chars": source_chars, "complete_scan": True, "extreme": extreme_result,
        "invalid_or_missing_values": {key: count for key, count in invalid.items() if count},
        "next_offset": str(offset + limit) if not selection.get("count_only") and offset + limit < len(selected) else None,
        "source_metadata": metadata,
        "notice": "Auswertung berechtigter Listenwerte, keine Live-Synchronisierung. Leere/ungueltige Werte sind bei min/max ausgeschlossen und bei Sortierung am Ende. Texte vergleichen ohne Gross-/Kleinschreibung; number ist Dezimalzahl, version numerische Punktversion, date ISO-Datum."}
