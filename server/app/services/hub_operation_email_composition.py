"""Email templates and composition discovered through the shared operation registry."""

from dataclasses import asdict
from functools import partial
import json
from urllib.parse import urlencode

from app.services.email_template_folders import EmailTemplateFolderService
from app.services.hub_email_composition import compose_context, forward_attachments, mailbox_for, normalize_reply_html, render_template
from app.services.hub_mailbox_transport import DEFAULT_HUB_MAILBOX_SENDER_EMAIL
from app.services.hub_operation_email_drafts import _save_draft
from app.services.hub_operations import HubArtifact, HubOperation, HubOperationError, HubOperationInputField as Field, HubOperationResult, HubOperationService, HubQuery, register_operation, register_query
from app.services.hub_record_access import identifier, require_actor
from app.services.hub_email_readers import template_library
from app.services.template_placeholders import EMAIL_TEMPLATE_CONTEXTS
from app.services.zoho_crm import ZohoCrmError


def safe_call(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except (ValueError, ZohoCrmError) as exc:
        raise HubOperationError(str(exc)) from exc


def _offset(values, name="offset"):
    raw = values.get(name) or "0"
    if not raw.isdecimal() or len(raw) > 7:
        raise HubOperationError("Ungueltiger Seitenbeginn.")
    return int(raw)


def _validate(values, fields):
    allowed = {field.name: field for field in fields}
    if set(values) - allowed.keys():
        raise HubOperationError("Unbekannte Eingaben.")
    for key, field in allowed.items():
        value = values.get(key, "")
        if not isinstance(value, str) or len(value) > (field.max_length or 255):
            raise HubOperationError(f"{field.label} ist ungueltig oder zu lang.")
        if field.required and not value.strip():
            raise HubOperationError(f"{field.label} fehlt.")
        if value and field.options and value not in dict(field.options):
            raise HubOperationError(f"{field.label} ist ungueltig.")


TEMPLATE_ID = Field("template_id", "Vorlagen-ID", required=True)
TEMPLATE_RENDER_FIELDS = (TEMPLATE_ID, Field("customer_id", "Kunde", context_type="customer"),
    Field("lead_id", "Lead", context_type="lead"), Field("recipient_key", "Kontakt-Schluessel"), Field("dunning_id", "Mahnung"),
    Field("context_module", "Kontextmodul", options=tuple((item.key, item.label) for item in EMAIL_TEMPLATE_CONTEXTS)),
    Field("context_record_id", "Kontext-Datensatz-ID"))
TEMPLATE_FIELDS = {
    "update": (TEMPLATE_ID, Field("name", "Name"), Field("subject", "Betreff", max_length=500), Field("content", "HTML-Vorlageninhalt", max_length=500_000), Field("context_module", "Kontext", options=tuple((item.key, item.label) for item in EMAIL_TEMPLATE_CONTEXTS)), Field("folder_name", "Ordner")),
    "clone": (TEMPLATE_ID, Field("name", "Name der Kopie", required=True)),
    "move": (Field("template_ids", "Vorlagen-IDs", required=True, max_length=100_000, encoding="JSON array of strings"), Field("folder_name", "Zielordner", required=True)),
    "delete": (Field("template_ids", "Vorlagen-IDs", required=True, max_length=100_000, encoding="JSON array of strings"),),
    "folders.create": (Field("name", "Ordnername", required=True),),
    "folders.delete": (Field("folder_id", "Ordner-ID", required=True),),
}


def manage_template(service, values, *, action):
    _validate(values, TEMPLATE_FIELDS[action])
    permission = "delete" if action.endswith("delete") else "create" if action in {"clone", "folders.create"} else "edit"
    require_actor(service, "emails", permission)
    communications = mailbox_for(service).communications
    folders = EmailTemplateFolderService(db=service.db)
    outputs = {}
    record_id = 0
    if action == "update":
        record = communications._stored_email_template(values["template_id"])
        payload = communications._payload(record.encrypted_payload_json)
        updated = communications.update_email_template(template_id=values["template_id"],
            name=values.get("name", payload.get("name", "")), subject=values.get("subject", payload.get("subject", "")),
            content=values.get("content", ""), context_module=values.get("context_module", ""),
            folder_name=values.get("folder_name"), preserve_content="content" not in values)
        outputs["template_id"] = updated.id
        record_id = record.id
    elif action == "clone":
        created = communications.clone_email_template(template_id=values["template_id"], name=values["name"])
        outputs["template_id"] = created.id
        record_id = communications._stored_email_template(created.id).id
    elif action in {"move", "delete"}:
        try:
            ids = json.loads(values["template_ids"])
        except ValueError as exc:
            raise HubOperationError("Ungueltige Vorlagenauswahl.") from exc
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
            raise HubOperationError("Ungueltige Vorlagenauswahl.")
        if action == "move":
            folder = folders.resolve_folder_name(name=values["folder_name"])
            count = communications.move_email_templates(template_ids=tuple(ids), folder_name=folder)
            outputs["folder_name"] = folder
        else:
            count = communications.delete_email_templates(template_ids=tuple(ids))
        outputs["changed_count"] = str(count)
    elif action == "folders.create":
        folder = folders.create_folder(name=values["name"])
        record_id = folder.id
        outputs.update(folder_name=folder.name, folder_id=str(folder.id))
    else:
        record_id = identifier(values["folder_id"])
        outputs["folder_name"] = folders.delete_empty_folder(folder_id=record_id,
            used_folder_names={item.category or item.module or "Weitere Vorlagen" for item in communications.list_email_templates()})
    href = "/email-templates"
    if outputs.get("template_id"):
        href += "?" + urlencode({"template": outputs["template_id"]})
    return HubOperationResult("Vorlagenbibliothek oeffnen", href, record_id, outputs=outputs)


def list_templates(service, values):
    require_actor(service, "emails", "view")
    templates = template_library(service)
    query = values.get("query", "").casefold()
    selected = [item for item in templates if query in f"{item.name} {item.subject} {item.category}".casefold()
        and (not values.get("folder_name") or item.category == values["folder_name"])
        and (not values.get("context_module") or item.context_module == values["context_module"])]
    offset = _offset(values)
    return {"items": [asdict(item) for item in selected[offset:offset + 25]], "total": len(selected),
        "next_offset": str(offset + 25) if len(selected) > offset + 25 else ""}


def read_template(service, values, *, rendered=False):
    require_actor(service, "emails", "view")
    detail = render_template(service, values) if rendered else mailbox_for(service).communications.get_email_template_source(template_id=values["template_id"])
    data = asdict(detail)
    start = _offset(values, "text_offset")
    data["content"] = detail.content[start:start + 6000]
    data["content_truncated"] = start > 0 or len(detail.content) > 6000
    data["next_text_offset"] = str(start + 6000) if len(detail.content) > start + 6000 else ""
    return data


def list_folders(service, values):
    require_actor(service, "emails", "view")
    folders = EmailTemplateFolderService(db=service.db).list_folders()
    offset = _offset(values)
    return {"items": [{"folder_id": str(item.id), "name": item.name} for item in folders[offset:offset + 25]],
        "total": len(folders), "next_offset": str(offset + 25) if len(folders) > offset + 25 else ""}


MESSAGE_FIELDS = (Field("email_key", "Original-E-Mail-Schluessel", required=True),
    Field("content", "Eigener Nachrichtentext (HTML, ohne Zitat/Signatur)", required=True, max_length=50_000),
    Field("sender_email", "Absenderadresse", max_length=320))
FORWARD_FIELDS = (*MESSAGE_FIELDS, Field("recipient_email", "Empfaengeradresse", required=True, max_length=320), Field("recipient_name", "Empfaengername"))


def message_draft(service, values, *, action):
    _validate(values, FORWARD_FIELDS if action == "forward" else MESSAGE_FIELDS)
    require_actor(service, "emails", "create")
    prepared = compose_context(service, values["email_key"], action)
    recipient = prepared.get("recipient") or {}
    files = forward_attachments(service, values["email_key"]) if action == "forward" else ()
    draft_service = HubOperationService(db=service.db, cipher=service.cipher, actor=service.actor,
        input_files=tuple(HubArtifact(filename=item.filename, content=item.content, content_type=item.content_type) for item in files))
    sender = values.get("sender_email") or DEFAULT_HUB_MAILBOX_SENDER_EMAIL
    target = values["recipient_email"] if action == "forward" else prepared["recipient_email"]
    cc = [address for address in prepared["cc_emails"] if address.casefold() not in {sender.casefold(), target.casefold()}]
    result = _save_draft(draft_service, {
        "sender_email": sender, "recipient_email": target,
        "recipient_name": values.get("recipient_name", "") if action == "forward" else recipient.get("name", ""),
        "recipient_key": "" if action == "forward" else recipient.get("key", ""),
        "customer_id": str(prepared.get("customer_id") or ""), "lead_id": str(prepared.get("lead_id") or ""),
        "subject": prepared["subject"], "content": normalize_reply_html(service.db, values["content"]) + prepared["content"],
        "cc_emails": ", ".join(cc), "reply_to_email_id": str(prepared.get("reply_to_email_id") or ""),
        # Forwarded files are now owned by the draft; do not append them a second time at send time.
        "forward_from_email_id": "",
    }, complete=False)
    return result


TEMPLATE_DRAFT_FIELDS = (*TEMPLATE_RENDER_FIELDS, Field("recipient_email", "Empfaengeradresse", required=True, max_length=320),
    Field("recipient_name", "Empfaengername"), Field("sender_email", "Absenderadresse", max_length=320),
    Field("attachment_ref", "Zusaetzlicher freigegebener Hub-Anhang"))


def template_draft(service, values):
    _validate(values, TEMPLATE_DRAFT_FIELDS)
    require_actor(service, "emails", "create")
    rendered = render_template(service, values)
    if rendered.unresolved_placeholders:
        raise HubOperationError("Die Vorlage benoetigt noch Werte fuer: " + ", ".join(rendered.unresolved_placeholders))
    return _save_draft(service, {**values, **(rendered.template_context or {}), "subject": rendered.subject, "content": rendered.content}, complete=True)


def read_composition(service, values):
    result = compose_context(service, values["email_key"], values["action"], allow_fetch=False)
    text = result["content"]
    start = _offset(values, "text_offset")
    result.update(content=text[start:start + 6000], next_text_offset=str(start + 6000) if len(text) > start + 6000 else "", content_truncated=start > 0 or len(text) > 6000)
    return result


for _action, _label in {"update": "E-Mail-Vorlage bearbeiten", "clone": "E-Mail-Vorlage kopieren", "move": "E-Mail-Vorlagen verschieben", "delete": "E-Mail-Vorlagen loeschen", "folders.create": "Vorlagenordner anlegen", "folders.delete": "Leeren Vorlagenordner loeschen"}.items():
    register_operation(HubOperation(key=f"emails.templates.{_action}", module="emails", label=_label,
        description=f"{_label}, wie in der Hub-Vorlagenbibliothek. Aendert keine Zoho-Quellvorlage.",
        input_guide="IDs aus emails.templates.list/read oder emails.templates.folders. Bearbeiten: ausgelassene Felder bleiben unveraendert, auch HTML und Styles. Zum Loeschen/Verschieben template_ids als JSON-Liste. Loeschen blendet importierte Vorlagen lokal aus, lokale Kopien werden entfernt.",
        input_fields=lambda action=_action: TEMPLATE_FIELDS[action], preview_fields=tuple((field.name, field.label) for field in TEMPLATE_FIELDS[_action]),
        execute=partial(safe_call, partial(manage_template, action=_action)),
        result_fields=(("template_id", "Vorlagen-ID (Text)"), ("folder_name", "Ordnername"), ("folder_id", "Ordner-ID"), ("changed_count", "Anzahl"))))
for _action, _label in {"reply": "Antwortentwurf", "reply_all": "Antwortentwurf an alle", "forward": "Weiterleitungsentwurf mit Originalanhaengen"}.items():
    _fields = FORWARD_FIELDS if _action == "forward" else MESSAGE_FIELDS
    register_operation(HubOperation(key=f"emails.drafts.{_action}", module="emails", label=_label,
        description=f"Erstellt einen unversendeten {_label} mit dem normalen E-Mail-Editor. Kein Versand.",
        input_guide="email_key aus emails.list/read oder ausgewaehltem Kontext. content nur eigener Nachrichtentext, ohne Zitat und Signatur. Empfaenger, Re:/Fwd:-Betreff und Zitat kommen aus der normalen Maske; bei forward Empfaenger angeben. Weiterleitung kopiert alle Originalanhaenge, Antwort nicht. Vorher emails.compose.preview zum Pruefen; fehlende Originalinhalte werden erst bei bestaetigter Erstellung nachgeladen.",
        input_fields=lambda fields=_fields: fields, preview_fields=tuple((field.name, field.label) for field in _fields),
        execute=partial(safe_call, partial(message_draft, action=_action))))
register_operation(HubOperation(key="emails.drafts.from_template", module="emails", label="Entwurf aus Vorlage",
    description="Setzt die Vorlage mit dem Kontext der E-Mail-Maske ein und speichert einen unversendeten Entwurf.",
    input_guide="Vorlage ueber emails.templates.list finden. Empfaenger sowie context_module und context_record_id angeben; Kunden-/Lead-Verknuepfungen werden daraus ermittelt. Optional Kundenkontakt oder PDF-attachment_ref. Fehlende Platzhalter werden benannt, niemals erfunden. Kein Versand.",
    input_fields=lambda: TEMPLATE_DRAFT_FIELDS, preview_fields=tuple((field.name, field.label) for field in TEMPLATE_DRAFT_FIELDS), execute=partial(safe_call, template_draft)))
register_query(HubQuery("emails.templates.list", "Lokale Vorlagen nach Name, Betreff, Ordner und Kontext suchen, 25 pro Seite.",
    (Field("query", "Suche"), Field("folder_name", "Ordner"), Field("context_module", "Kontext"), Field("offset", "Seitenbeginn")), partial(safe_call, list_templates)))
register_query(HubQuery("emails.templates.read", "Vorlagenquelle mit Platzhaltern lesen. Grosse Inhalte ueber next_text_offset vollstaendig nachladen; bei Updates nicht gekuerzte Inhalte uebernehmen.",
    (TEMPLATE_ID, Field("text_offset", "Textbeginn")), partial(safe_call, read_template)))
register_query(HubQuery("emails.templates.render", "Vorlage wie in der Maske aus dem autorisierten Kontextdatensatz und seinen Beziehungen aufloesen. Kunden, Leads, Finance, Faelle, Aktivitaeten und Sites. Unaufgeloeste Platzhalter werden benannt. Keine Schreibaktionen.",
    (*TEMPLATE_RENDER_FIELDS, Field("recipient_email", "Empfaengeradresse", max_length=320), Field("text_offset", "Textbeginn")), partial(safe_call, partial(read_template, rendered=True))))
register_query(HubQuery("emails.templates.folders", "Vorhandene Vorlagenordner lesen, ohne Ordner anzulegen.", (Field("offset", "Seitenbeginn"),), partial(safe_call, list_folders)))
register_query(HubQuery("emails.compose.preview", "Empfaenger, Betreff, Signatur und Zitat fuer Antwort/Weiterleitung lokal vorbereiten, ohne Originalinhalte extern zu laden oder Lesestatus zu aendern.",
    (Field("email_key", "E-Mail-Schluessel", required=True), Field("action", "Vorbereitung", required=True, options=(("reply", "Antwort"), ("reply_all", "Allen antworten"), ("forward", "Weiterleiten"))), Field("text_offset", "Textbeginn")), partial(safe_call, read_composition)))
