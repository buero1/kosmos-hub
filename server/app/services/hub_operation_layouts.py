"""Global layouts: UI and agent use the same field catalog and persistence."""
from dataclasses import dataclass, asdict
from hashlib import sha256
import json

from sqlalchemy import select

from app.models.module_layout import ModuleLayout
from app.services.module_layout_catalog import get_module_layout_definition, module_layout_definitions
from app.services.module_layouts import ModuleLayoutService
from app.services.hub_operations import HubOperation, HubOperationError, HubOperationInputField as Field, HubOperationResult, HubQuery, register_operation, register_query
from app.services.hub_record_access import require_actor
from app.services.hub_operation_queries import _offset


def require_admin(service):
    user, _ = require_actor(service, "settings", "manage")
    if user.role != "admin":
        raise HubOperationError("Nur Hub-Administratoren koennen globale Layouts verwalten.")
    return user


@dataclass(frozen=True)
class LayoutView:
    definition: object
    fields: tuple
    show_more_index: int
    order_json: str
    revision: str


def layout_view(service, layout_key):
    require_admin(service)
    definition = get_module_layout_definition(layout_key)
    if definition is None:
        raise HubOperationError("Das Modullayout wurde nicht gefunden.")
    fields = {field.key: field for field in definition.fields}
    keys, index = ModuleLayoutService(db=service.db).ordered_keys_with_show_more(
        layout_key=layout_key, default_keys=tuple(fields), default_show_more_after=definition.default_show_more_after)
    order = [*keys[:index], ModuleLayoutService.SHOW_MORE_ITEM_KEY, *keys[index:]]
    encoded = json.dumps(order, ensure_ascii=True, separators=(",", ":"))
    return LayoutView(definition, tuple(fields[key] for key in keys), index, encoded, sha256(encoded.encode()).hexdigest())


def update_layout(service, values):
    actor = require_admin(service)
    allowed = {"layout_key", "order_json", "expected_revision"}
    if set(values) - allowed or any(not isinstance(value, str) for value in values.values()):
        raise HubOperationError("Unbekannte oder ungueltige Layouteingabe.")
    key = values.get("layout_key", "")
    service.db.scalar(select(ModuleLayout).where(ModuleLayout.layout_key == key)
        .with_for_update().execution_options(populate_existing=True))
    view = layout_view(service, key)
    if values.get("expected_revision") and values["expected_revision"] != view.revision:
        raise HubOperationError("Das Layout wurde inzwischen geaendert. Bitte erneut laden und die Aenderungen abgleichen.")
    record = ModuleLayoutService(db=service.db).configure(actor=actor, layout_key=key,
        item_order_json=values.get("order_json", ""), allowed_keys=tuple(field.key for field in view.definition.fields))
    return HubOperationResult(view.definition.label, f"/module-layouts/{key}?saved=true", record.id,
        outputs={"layout_key": key, "revision": layout_view(service, key).revision})


def list_layouts(service, values):
    require_admin(service)
    return {"items": [{"layout_key": item.layout_key, "label": item.label,
        "field_count": len(item.fields), "href": f"/module-layouts/{item.layout_key}"}
        for item in module_layout_definitions()]}


def read_layout(service, values):
    view = layout_view(service, values["layout_key"])
    start = _offset(values, "offset")
    return {"layout_key": view.definition.layout_key, "label": view.definition.label,
        "fields": [asdict(field) for field in view.fields[start:start + 25]], "total": len(view.fields),
        "next_offset": str(start + 25) if len(view.fields) > start + 25 else "",
        "order_json": view.order_json, "show_more_index": view.show_more_index, "revision": view.revision,
        "notice": "Globale Darstellung fuer alle Datensaetze des Moduls, keine Datenaenderung oder Zugriffsfreigabe."}


LAYOUT_KEY = Field("layout_key", "Modullayout", required=True,
    options=tuple((item.layout_key, item.label) for item in module_layout_definitions()))
register_query(HubQuery("layouts.list", "Verfuegbare globale Modullayouts aus demselben Feldkatalog wie die Masken. Nur Admin.", (), list_layouts))
register_query(HubQuery("layouts.read", "Aktuelle Feldreihenfolge, Mehr-anzeigen-Grenze und Revision. Feldbeschreibungen in Seiten zu 25, order_json ist vollstaendig.",
    (LAYOUT_KEY, Field("offset", "Feldbeginn")), read_layout))
register_operation(HubOperation(key="layouts.update", module="settings", label="Globales Modullayout bearbeiten",
    description="Aendert die Feldreihenfolge und Mehr-anzeigen-Grenze fuer alle Ansichten eines Moduls, nur fuer Admins.",
    input_guide="Zuerst layouts.read verwenden, bei Bedarf alle Feldseiten lesen. Vollstaendiges order_json neu anordnen: jedes Feld genau einmal, __show_more__ markiert die Grenze. expected_revision aus dem gelesenen Layout mitgeben. Keine Feldwerte oder Rechte werden geaendert.",
    input_fields=lambda: (LAYOUT_KEY, Field("order_json", "Vollstaendige Feldreihenfolge", required=True, max_length=25_000, encoding="JSON array of field keys and __show_more__"),
        Field("expected_revision", "Gelesene Layoutrevision", max_length=64)),
    preview_fields=(("layout_key", "Globales Layout"), ("order_json", "Neue Reihenfolge inkl. Mehr-anzeigen-Grenze")),
    execute=update_layout, result_fields=(("layout_key", "Layout"), ("revision", "Neue Revision"))))
