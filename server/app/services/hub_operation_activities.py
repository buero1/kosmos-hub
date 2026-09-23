"""One activity gateway for customer/lead drawers, the calendar and the agent."""

from dataclasses import replace
from datetime import UTC
from functools import partial
from typing import Mapping
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select, delete

from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerMeetingActivity, CustomerTaskActivity
from app.models.hub_case import HubCase
from app.models.hub_lead import HubLead
from app.models.hub_user import HubUser
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.services.customer_activities import CustomerActivityService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_activity_catalog import ACTIVITY_KINDS, activity_defaults, activity_fields
from app.services.hub_activity_responsibility import ActivityResponsibility, record_completion
from app.services.hub_leads import HubLeadService
from app.services.hub_operations import (
    HubOperation, HubOperationError, HubOperationInputField, HubOperationResult,
    HubOperationService, register_operation,
)
from app.services.task_email_reminders import TaskEmailReminderService, TaskEmailReminderError
from app.services.task_email_reminder_worker import TaskEmailReminderWorker


ACTIVITY_MODELS = {"call": CustomerCallActivity, "task": CustomerTaskActivity, "meeting": CustomerMeetingActivity}


def _id(value: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    if not raw.isdecimal() or int(raw) < 1:
        raise HubOperationError("Die Datensatz-ID ist ungültig.")
    return int(raw)


def _owner(service, values, user, access) -> dict[str, int | None]:
    owner = {"customer_id": None, "lead_id": None}
    for kind, model, module in (("customer", Customer, "customers"), ("lead", HubLead, "leads")):
        identifier = _id(values.get(f"{kind}_id", ""))
        name = values.get(f"{kind}_name", "").strip()
        if identifier is None and name:
            allowed = access.accessible_record_ids(user=user, module_key=module)
            if kind == "customer":
                query = select(Customer).where(Customer.name == name)
                if allowed is not None:
                    query = query.where(Customer.id.in_(allowed))
                matches = {item.id for item in service.db.scalars(query)}
            else:
                matches = {
                    item.lead.id for item in HubLeadService(db=service.db, cipher=service.cipher).list_leads(allowed_lead_ids=allowed)
                    if name.casefold() in {item.name.casefold(), item.company.casefold()}
                }
            if len(matches) != 1:
                raise HubOperationError("Die Verknüpfung wurde nicht gefunden oder ist nicht eindeutig.")
            identifier = matches.pop()
        if identifier is not None and (
            not access.can_access_record(user=user, module_key=module, record_id=identifier)
            or service.db.get(model, identifier) is None
        ):
            raise HubOperationError("Der verknüpfte Datensatz ist nicht verfügbar.")
        owner[f"{kind}_id"] = identifier
    if all(value is not None for value in owner.values()):
        raise HubOperationError("Bitte nur einen Kunden oder Lead verknüpfen.")
    return owner


def _can_access(activity, user, access, db) -> bool:
    return ActivityResponsibility(db, user).visible(activity)


def _activity(service, kind, values, owner, user, access):
    identifier = _id(values.get("activity_id", ""))
    name = values.get("target_name", "").strip()
    model = ACTIVITY_MODELS[kind]
    if identifier is None and not name:
        raise HubOperationError("Bitte die Aktivitäts-ID oder einen eindeutigen Aktivitätsnamen angeben.")
    query = select(model).where(model.id == identifier if identifier is not None else model.name == name)
    for key, value in owner.items():
        if value is not None:
            query = query.where(getattr(model, key) == value)
    policy = ActivityResponsibility(service.db, user)
    matches = [item for item in service.db.scalars(query.with_for_update()) if policy.visible(item)]
    if len(matches) != 1:
        raise HubOperationError("Die Aktivität wurde nicht gefunden oder ist nicht eindeutig.")
    return matches[0]


def _existing_values(kind, activity) -> dict[str, str]:
    values = {"name": activity.name, "status": activity.status, "description": activity.description or "",
              "assignee_user_id": str(activity.assignee_user_id or "")}
    timestamp = activity.due_at if kind == "task" else activity.starts_at
    prefix = "due" if kind == "task" else "start"
    local = timestamp.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin")) if timestamp is not None else None
    values.update({f"{prefix}_date": local.strftime("%Y-%m-%d") if local else "",
                   f"{prefix}_time": local.strftime("%H:%M") if local else ""})
    if kind == "task":
        values.update(reminder_channel=activity.reminder_channel or "none", reminder_minutes_before=str(activity.reminder_minutes_before or 0))
    else:
        duration = activity.duration_minutes if kind == "call" else (
            int((activity.ends_at - activity.starts_at).total_seconds() // 60)
            if activity.ends_at is not None and activity.starts_at is not None else 60
        )
        values.update(
            duration_minutes=str(duration),
            reminder_channels=",".join(item.channel for item in activity.reminders),
            reminder_minutes_before=",".join(str(item.minutes_before) for item in activity.reminders),
        )
    if kind == "call":
        values["direction"] = activity.direction
    return values


def _fields(kind: str, values: Mapping[str, str]) -> dict:
    fields = {field.name: values.get(field.name, "") for field in activity_fields(kind)}
    for definition in activity_fields(kind):
        if definition.required and not fields[definition.name].strip():
            raise HubOperationError(f"{definition.label} fehlt.")
        if definition.multiple:
            raw = fields[definition.name]
            fields[definition.name] = [item.strip() for item in raw.split(",")] if raw.strip() else []
    fields.pop("assignee_user_id", None)
    return fields


def _execute(service: HubOperationService, values: Mapping[str, str], *, kind: str, action: str) -> HubOperationResult:
    # Failed validation must not leave partial edits or a half-created activity behind.
    with service.db.begin_nested():
        return _execute_atomic(service, values, kind=kind, action=action)


def _execute_atomic(service: HubOperationService, values: Mapping[str, str], *, kind: str, action: str) -> HubOperationResult:
    user = service.db.scalar(select(HubUser).where(HubUser.username == service.actor, HubUser.is_active.is_(True)))
    access = HubAccessControlService(db=service.db)
    permission = {"create": "create", "update": "edit", "complete": "edit", "delete": "delete"}[action]
    if user is None or not access.can(user, "activities", permission):
        raise HubOperationError("Für diese Aktivität fehlt die Berechtigung.")
    owner = _owner(service, values, user, access)
    policy = ActivityResponsibility(service.db, user)
    activities = CustomerActivityService(db=service.db)
    previous_status = None
    if action == "create":
        target = policy.assignee(values.get("assignee_user_id") or str(user.id), SimpleNamespace(**owner, assignee_user_id=None))
        activity = getattr(activities, f"schedule_{kind}")(actor=service.actor, assignee_user_id=target.id, **owner, **_fields(kind, values))
    else:
        activity = _activity(service, kind, values, owner, user, access)
        if not policy.allowed(activity, permission):
            raise HubOperationError("Zum Bearbeiten fremder Aktivitaeten ist das Recht Verwalten erforderlich.")
        try:
            TaskEmailReminderService(db=service.db).assert_not_sending(activity=activity, kind=kind)
        except TaskEmailReminderError as exc:
            raise HubOperationError(str(exc)) from exc
        previous_status = activity.status
        previous_assignee = activity.assignee_user_id
        previous_values = _existing_values(kind, activity)
        if action == "update":
            if "new_customer_id" in values or "new_lead_id" in values:
                if kind == "task":
                    raise HubOperationError("Die Verknüpfung einer Aufgabe wird hier nicht geändert.")
                new_owner = {"customer_id": str(activity.customer_id or ""), "lead_id": str(activity.lead_id or "")}
                for key in tuple(new_owner):
                    if f"new_{key}" in values:
                        new_owner[key] = values[f"new_{key}"]
                if values.get("new_customer_id"):
                    new_owner["lead_id"] = values.get("new_lead_id", "")
                if values.get("new_lead_id"):
                    new_owner["customer_id"] = values.get("new_customer_id", "")
                resolved_owner = _owner(service, new_owner, user, access)
                for key, value in resolved_owner.items():
                    setattr(activity, key, value)
            if "assignee_user_id" in values or activity.assignee_user_id is not None:
                target = policy.assignee(values.get("assignee_user_id", str(activity.assignee_user_id)), activity)
                activity.assignee_user_id = target.id
            fields = _fields(kind, {**_existing_values(kind, activity), **values})
            activity = getattr(activities, f"update_directory_{kind}")(**{f"{kind}_id": activity.id}, **fields)
        elif action == "complete":
            if activity.status == "planned":
                activity.status = "completed"
                service.db.flush()
                if kind == "task":
                    activities._sync_task_email_reminder(activity)
        elif action == "delete":
            TaskEmailReminderService(db=service.db).cancel_activity(activity=activity, kind=kind)
            service.db.delete(activity)
            service.db.flush()
        reminder_keys = {"due_date", "due_time", "start_date", "start_time", "reminder_channels", "reminder_channel", "reminder_minutes_before"}
        if action == "delete" or previous_assignee != activity.assignee_user_id or any(
                previous_values.get(key) != _existing_values(kind, activity).get(key) for key in reminder_keys):
            service.db.execute(delete(CustomerActivityReminderNotification).where(
                CustomerActivityReminderNotification.activity_kind == kind,
                CustomerActivityReminderNotification.activity_id == activity.id))
    if action != "delete":
        record_completion(activity, user=user, previous_status=previous_status)
        try:
            TaskEmailReminderService(db=service.db).sync_activity(activity=activity, kind=kind)
        except TaskEmailReminderError as exc:
            raise HubOperationError(str(exc)) from exc
        service.db.flush()
    service.after_commit("task-reminders", TaskEmailReminderWorker.notify_schedule_changed)
    plural, label = ACTIVITY_KINDS[kind]
    href = f"/{plural}"
    if activity.customer_id is not None:
        href = f"/customers/{activity.customer_id}#customer-activities"
    elif activity.lead_id is not None:
        href = f"/leads/{activity.lead_id}#lead-activities"
    return HubOperationResult(
        label=f"{label} öffnen" if action != "delete" else "Aktivitäten öffnen", href=href,
        record_id=activity.id,
        outputs={"activity_id": str(activity.id), "customer_id": str(activity.customer_id or ""), "lead_id": str(activity.lead_id or ""),
                 "assignee_user_id": str(activity.assignee_user_id or "")},
    )


def _input_fields(kind: str, action: str) -> tuple[HubOperationInputField, ...]:
    relation = tuple(HubOperationInputField(key, label, context_type="customer" if key == "customer_name" else "") for key, label in (
        ("customer_id", "Kunden-ID"), ("lead_id", "Lead-ID"), ("customer_name", "Kundenname"), ("lead_name", "Leadname"),
    ))
    if action == "create":
        return relation + activity_fields(kind)
    target = (HubOperationInputField("activity_id", "Aktivitäts-ID"), HubOperationInputField("target_name", "Vorhandener Aktivitätsname"))
    if action == "update":
        relink = (
            HubOperationInputField("new_customer_id", "Neue Kundenverknüpfung"),
            HubOperationInputField("new_lead_id", "Neue Leadverknüpfung"),
        ) if kind != "task" else ()
        return relation + target + tuple(replace(field, required=False) for field in activity_fields(kind)) + relink
    return relation + target


for _kind, (_plural, _label) in ACTIVITY_KINDS.items():
    for _action, _verb in (("create", "anlegen"), ("update", "bearbeiten"), ("complete", "abschließen"), ("delete", "löschen")):
        if _kind == "meeting" and _action == "complete":
            continue  # The UI changes meeting status through the edit operation.
        _guide = (
            "Verantwortlich ist beim Anlegen standardmaessig der angemeldete Benutzer, nicht ein erfundener Name. "
            "Andere Benutzer ueber activities.assignees ermitteln; Zuweisung erweitert keine Kunden-/Leadrechte. "
            "Optional höchstens ein Kunde oder Lead: customer_id oder lead_id; alternativ eindeutiger customer_name oder lead_name. "
            "Ohne Bezug bleiben Aktivitäten unverknüpft. Datum YYYY-MM-DD und Uhrzeit HH:MM in Europe/Berlin. "
            "Bei Anrufen und Meetings sind reminder_channels und reminder_minutes_before parallele, kommagetrennte Listen (maximal 5). "
        )
        if _action != "create":
            _guide += (
                "Vorhandene Aktivität über activity_id oder eindeutigen target_name auswählen; Kunden-/Leadangaben grenzen die Auswahl ein. "
            )
            if _action == "update":
                _reminder_key = "reminder_channel" if _kind == "task" else "reminder_channels"
                _guide += f"Weggelassene Felder bleiben unverändert; description='' leert die Beschreibung und {_reminder_key}='none' entfernt Erinnerungen. "
            if _kind != "task" and _action == "update":
                _guide += "Neue Verknüpfungen nur mit new_customer_id oder new_lead_id, leer entfernt den jeweiligen Bezug. "
        register_operation(HubOperation(
            key=f"activities.{_plural}.{_action}", module="activities", label=f"{_label} {_verb}",
            description=f"{_label} {_verb}, mit derselben Prüfung und Speicherung wie in der Hub-Maske.",
            input_guide=_guide,
            preview_fields=tuple((field.name, field.label) for field in _input_fields(_kind, _action)),
            execute=partial(_execute, kind=_kind, action=_action),
            defaults=(lambda values, kind=_kind: activity_defaults(kind)) if _action == "create" else None,
            input_fields=partial(_input_fields, _kind, _action),
            result_fields=(("activity_id", "Aktivitäts-ID"), ("customer_id", "Verknüpfter Kunde, sonst leer"), ("lead_id", "Verknüpfter Lead, sonst leer"), ("assignee_user_id", "Verantwortlicher Benutzer")),
        ))
