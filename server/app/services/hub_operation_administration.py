"""Discoverable settings and admin actions backed by the UI's native boundary."""
from dataclasses import asdict
from functools import partial
import json

from app.services.hub_administration import HubAdministrationService, SETTING_SERVICES, runtime_settings, setting_parameters
from app.services.hub_access_control import ACCESS_MODULES, ACCESS_ACTIONS, ACCESS_SCOPES
from app.services.email_ai_prompt_presets import EmailAiPromptPresetInput, DEFAULT_EMAIL_AI_PROMPT_PRESETS
from app.services.hub_operations import HubOperation, HubOperationError, HubOperationInputField as Field, HubOperationResult, HubQuery, register_operation, register_query
from app.services.hub_operation_queries import _offset
from app.services.hub_record_access import identifier
from app.services.ai_models import MODEL_PROFILES
from app.services.ai_provider import AiProviderConfigService, AiProviderConfigError
from app.services.audit import write_audit_log


def domain(service):
    return HubAdministrationService(db=service.db, cipher=service.cipher, actor=service.actor)


def field(name, required=False, *, max_length=1000, encoding=""):
    return Field(name, name.replace("_", " "), required=required, max_length=max_length, encoding=encoding)


def validate(values, fields):
    allowed = {item.name: item for item in fields}
    if set(values) - allowed.keys():
        raise HubOperationError("Unbekannte Verwaltungseingabe.")
    for name, definition in allowed.items():
        value = values.get(name, "")
        if not isinstance(value, str) or len(value) > (definition.max_length or 1000):
            raise HubOperationError(f"Ungueltiger Wert fuer {name}.")
        if definition.required and not value.strip():
            raise HubOperationError(f"{definition.label} fehlt.")


def parsed(value, expected):
    try:
        result = json.loads(value)
    except ValueError as exc:
        raise HubOperationError("Ungueltige JSON-Eingabe.") from exc
    if not isinstance(result, expected):
        raise HubOperationError("Ungueltige JSON-Struktur.")
    return result


def read_settings(service, values):
    native = domain(service)
    user = native.user(admin=False)
    section = values.get("section") or "personal"
    if section == "personal":
        return {"user_id": str(user.id), "username": user.username, "reminder_email": user.reminder_email or ""}
    native.user()
    if section == "ai_model":
        config = AiProviderConfigService(db=service.db, cipher=service.cipher).get_openai_config()
        return {"model": config.model if config else None,
                "models": [{"model": profile.model, "label": profile.label, "pricing": profile.pricing.snapshot()}
                           for profile in MODEL_PROFILES]}
    if section in SETTING_SERVICES:
        current = runtime_settings(service.db, section)
        return {"section": section, "values": {name: getattr(current, name) for name in setting_parameters(section)},
                "input_fields": {name: {"type": p.annotation.__name__, "required": False} for name, p in setting_parameters(section).items()}}
    if section == "signature":
        text = runtime_settings(service.db, "email_composer").signature_html
        start = _offset(values, "offset")
        return {"signature_html": text[start:start + 6000], "next_offset": str(start + 6000) if len(text) > start + 6000 else ""}
    if section == "mailbox_alert":
        return {"mailbox_alert_email": user.mailbox_alert_email or ""}
    presets = native.prompt_presets()
    start = _offset(values, "offset")
    return {"items": [{"preset_id": item.id, "label": item.label, "instruction": item.instruction, "is_enabled": item.is_enabled}
                      for item in presets[start:start + 25]], "next_offset": str(start + 25) if len(presets) > start + 25 else "",
            "unsaved_defaults": [] if presets else [asdict(item) for item in DEFAULT_EMAIL_AI_PROMPT_PRESETS],
            "notice": "Nicht gespeicherte Standards haben keine IDs. settings.email_prompts.update initialisiert sie nach Bestaetigung; danach erneut lesen."}


def settings_fields(section):
    if section in SETTING_SERVICES:
        return tuple(field(name) for name in setting_parameters(section))
    return {
        "ai_model": (Field("model", "KI-Modell", required=True,
                           options=tuple((profile.model, profile.label) for profile in MODEL_PROFILES)),),
        "signature": (field("signature_html", max_length=50000),),
        "personal": (field("reminder_email", True),),
        "mailbox_alert": (field("mailbox_alert_email", True),),
        "email_prompts": (field("presets", max_length=100000, encoding='JSON array: {preset_id: integer, label: string, instruction: string, is_enabled: boolean}'), field("new_label"), field("new_instruction")),
    }[section]


def update_settings(service, values, *, section):
    validate(values, settings_fields(section))
    native = domain(service)
    native.user(admin=section != "personal")
    if section == "signature" and "signature_html" not in values:
        raise HubOperationError("Signatur fehlt; zum Loeschen explizit leer angeben.")
    if section == "ai_model":
        provider = AiProviderConfigService(db=service.db, cipher=service.cipher)
        previous = provider.get_openai_config()
        previous_model = previous.model if previous else "not-configured"
        try:
            config = provider.select_openai_model(actor=native.user(), model=values["model"])
        except AiProviderConfigError as exc:
            raise HubOperationError(str(exc)) from exc
        write_audit_log(service.db, site=None, actor=service.actor, source="hub-account",
            action="select-openai-model", result="success", detail=f"OpenAI model: {previous_model} -> {config.model}.")
        return HubOperationResult("KI-Modell gespeichert", "/account#account-openai", config.id)
    if section in SETTING_SERVICES:
        native.configure(section, **values)
    elif section == "signature":
        native.configure_signature(signature_html=values.get("signature_html", ""))
    elif section == "personal":
        native.configure_reminder_email(reminder_email=values.get("reminder_email", ""))
    elif section == "mailbox_alert":
        native.configure_mailbox_alert(mailbox_alert_email=values["mailbox_alert_email"])
    else:
        presets = parsed(values.get("presets") or "[]", list)
        if len(presets) > 100 or any(not isinstance(item, dict) or set(item) != {"preset_id", "label", "instruction", "is_enabled"}
            or type(item["preset_id"]) is not int or type(item["is_enabled"]) is not bool
            or not isinstance(item["label"], str) or not isinstance(item["instruction"], str) for item in presets):
            raise HubOperationError("Ungueltige Schnellaktionen.")
        native.save_prompt_presets(presets=tuple(EmailAiPromptPresetInput(**item) for item in presets),
                                  new_label=values.get("new_label", ""), new_instruction=values.get("new_instruction", ""))
    return HubOperationResult("Einstellungen gespeichert", "/account" if section == "personal" else "/agent#agent-email-ai-prompts" if section == "email_prompts" else "/settings", 0)


def read_access(service, values):
    snapshot = domain(service).access_snapshot()
    section = values["section"]
    start = _offset(values, "offset")
    items = snapshot[section]
    columns = {"users": ("id", "username", "first_name", "last_name", "display_name", "role", "team_id", "is_active", "reminder_email", "email_address", "default_sender_account_id"),
               "roles": ("key", "name", "description", "is_system"), "teams": ("id", "name", "description"),
               "assignments": ("id", "module_key", "record_id", "owner_user_id", "team_id"),
               "grants": ("id", "module_key", "record_id", "user_id", "team_id", "can_edit")}[section]
    projected = [{key: getattr(row, key) for key in columns} for row in items[start:start + 25]]
    if section == "roles":
        for item in projected:
            item["permissions"] = {module: {**{action: bool(getattr(p, f"can_{action}")) for action in ACCESS_ACTIONS}, "scope": p.record_scope}
                                   for module, p in snapshot["permissions"].get(item["key"], {}).items()}
    return {"items": projected, "total": len(items), "next_offset": str(start + 25) if len(items) > start + 25 else "",
            "modules": [asdict(module) for module in ACCESS_MODULES], "actions": ACCESS_ACTIONS, "scopes": ACCESS_SCOPES,
            "notice": "Nur Admin. Keine Passwoerter, Hashes, Tokens oder Schluessel. Ausgelassene Rollenrechte bleiben erhalten."}


ACCESS_FIELDS = {
    "roles.save": (field("role_key"), field("name", True), field("description"), field("permissions", True, max_length=30000, encoding='JSON object: module_key -> {view/create/edit/send/delete/export/manage: boolean, scope: none/assigned/team/all}; send applies to emails')),
    "roles.delete": (field("role_key", True),),
    "teams.create": (field("name", True), field("description")),
    "teams.update": (field("team_id", True), field("name", True), field("description")),
    "teams.delete": (field("team_id", True),),
    "users.update": (field("user_id", True), field("username"), field("first_name", max_length=100), field("last_name", max_length=100), field("role"), field("team_id"), field("reminder_email"), field("email_address"), field("default_sender_account_id")),
    "users.delete": (field("user_id", True),),
    "records.assign": (field("module_key", True), field("record_id", True), field("owner_user_id"), field("team_id")),
    "grants.create": (field("module_key", True), field("record_id", True), field("user_id"), field("team_id"), field("can_edit")),
    "grants.delete": (field("grant_id", True),),
}


def update_access(service, values, *, action):
    validate(values, ACCESS_FIELDS[action])
    native = domain(service)
    native.user()
    data = dict(values)
    for name in ("user_id", "team_id", "record_id", "owner_user_id", "grant_id"):
        if name in data:
            data[name] = identifier(data[name])
    if action == "roles.save":
        data["permissions"] = parsed(data["permissions"], dict)
        data.setdefault("role_key", "")
        data.setdefault("description", "")
        result = native.save_role(**data)
    elif action == "roles.delete":
        result = native.delete_role(**data)
    elif action == "teams.create":
        result = native.create_team(**data)
    elif action == "teams.update":
        result = native.update_team(**data)
    elif action == "teams.delete":
        result = native.delete_team(**data)
    elif action == "users.update":
        result = native.update_user(**data)
    elif action == "users.delete":
        native.delete_user(**data)
        result = None
    elif action == "records.assign":
        data.setdefault("owner_user_id", None)
        data.setdefault("team_id", None)
        result = native.assign_record(**data)
    elif action == "grants.create":
        if data.get("can_edit", "false") not in {"true", "false"}:
            raise HubOperationError("can_edit muss true oder false sein.")
        data["can_edit"] = data.get("can_edit") == "true"
        data.setdefault("user_id", None)
        data.setdefault("team_id", None)
        result = native.add_grant(**data)
    else:
        result = native.delete_grant(**data)
    return HubOperationResult("Verwaltung gespeichert", "/settings#account-access", getattr(result, "id", 0) or 0,
                              outputs={"role_key": getattr(result, "key", "")})


def read_mailbox_access(service, values):
    return domain(service).mailbox_access_snapshot()


def update_mailbox_access(service, values):
    entries = parsed(values.get("permissions", "{}"), dict)
    domain(service).save_mailbox_access(subject_type=values["subject_type"], subject_id=identifier(values["subject_id"]), entries=entries)
    return HubOperationResult("Postfachzugriff gespeichert", "/settings#account-access", identifier(values["subject_id"]))


register_query(HubQuery("access.mailboxes.read", "Postfachfreigaben von Teams und Benutzern inklusive effektiver Rechte lesen. Nur Admin; keine Zugangsdaten.", (), read_mailbox_access))
register_operation(HubOperation(key="access.mailboxes.update", module="settings", label="Postfachzugriff ändern",
    description="Postfachfreigaben wie in der Team-/Benutzermaske speichern. Nur Admin. Rollen bleiben die Obergrenze; Benutzer-Sperren überschreiben Teamfreigaben.",
    input_guide='Vorher access.mailboxes.read. permissions: JSON Objekt Postfach-ID -> {view, create, edit, send, delete}, alle Werte boolesch; beim Benutzer null für Team erben. Nur angegebene Postfächer ändern sich.',
    input_fields=lambda: (Field("subject_type", "Zieltyp", required=True, options=(("user", "Benutzer"), ("team", "Team"))),
        field("subject_id", True), field("permissions", True, max_length=100000, encoding="JSON object")),
    preview_fields=(("subject_type", "Zieltyp"), ("subject_id", "Ziel-ID"), ("permissions", "Postfachrechte")), execute=update_mailbox_access))


register_query(HubQuery("settings.read", "Persoenliche Erinnerungseinstellung oder globale Hub-Einstellungen lesen, globale Bereiche nur Admin. Keine Zugangsdaten. Offset fuer Signaturtext/Schnellaktionen.",
    (Field("section", "Bereich", options=tuple((key, key) for key in (*SETTING_SERVICES, "personal", "signature", "mailbox_alert", "email_prompts", "ai_model"))), field("offset")), read_settings))
register_query(HubQuery("access.read", "Benutzer, Rollenrechte, Teams, Datensatzzuordnungen und Freigaben lesen, nur Admin, 25 Eintraege pro Seite.",
    (Field("section", "Bereich", required=True, options=tuple((key, key) for key in ("users", "roles", "teams", "assignments", "grants"))), field("offset")), read_access))
for section in (*SETTING_SERVICES, "personal", "signature", "mailbox_alert", "email_prompts", "ai_model"):
    register_operation(HubOperation(key=f"settings.{section}.update", module="settings", label=f"Einstellungen: {section}",
        description="Einstellung nach Bestaetigung mit derselben Validierung wie die Maske speichern. Globale Bereiche nur Admin; personal betrifft nur den handelnden Benutzer.",
        input_guide="Vorher settings.read verwenden. Bei Styling, E-Mail-Editor und Fleet nur angegebene Werte aendern; andere bleiben erhalten. Explizit leere Signatur loescht sie. Persoenliche Adresse muss gueltig sein. Keine Zugangsdaten.",
        input_fields=partial(settings_fields, section), preview_fields=(), preview_builder=lambda values: tuple(f"{key}: {value}" for key, value in values.items()),
        execute=partial(update_settings, section=section)))
for action, fields in ACCESS_FIELDS.items():
    register_operation(HubOperation(key=f"access.{action}", module="settings", label=f"Verwaltung: {action}",
        description="Administrative Aenderung nach ausdruecklicher Bestaetigung, nur als aktueller Admin. Schutz des letzten Admins, Systemrollen und Team-/Datensatzpruefungen wie in der Maske.",
        input_guide="Vorher access.read verwenden. Exakte IDs benutzen. Bei users.update nur angegebene Felder aendern; leere team_id entfernt Team. Rollenrechte werden teilweise aktualisiert; boolesche JSON-Werte, keine Texte. Nie Passwoerter eingeben; Benutzeranlage und Passwortaenderung bleiben in der sicheren Maske.",
        input_fields=lambda fields=fields: fields, preview_fields=(), preview_builder=lambda values: tuple(f"{key}: {value}" for key, value in values.items()),
        execute=partial(update_access, action=action), result_fields=(("role_key", "Rollenschluessel"),)))
