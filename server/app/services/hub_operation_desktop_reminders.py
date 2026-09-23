"""Desktop delivery and agent preview share reminder state and authorized actions."""
from functools import partial
import json

from sqlalchemy import select

from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.services.customer_desktop_reminders import CustomerDesktopReminderService, SNOOZE_MINUTES_OPTIONS
from app.services.hub_operations import HubOperation, HubOperationError, HubOperationInputField as Field, HubOperationResult, HubQuery, register_operation, register_query
from app.services.hub_record_access import require_actor
from app.services.hub_operation_queries import _offset


def reminder_views(service, *, deliver=False):
    user, _ = require_actor(service, "activities", "view")
    return CustomerDesktopReminderService(db=service.db).list_due_reminders(user=user, materialize=deliver)


def read_reminders(service, values):
    rows = reminder_views(service)
    start = _offset(values, "offset")
    return {"items": [row.as_dict() for row in rows[start:start + 25]], "total": len(rows),
            "next_offset": str(start + 25) if len(rows) > start + 25 else "", "snooze_minutes_options": SNOOZE_MINUTES_OPTIONS,
            "notice": "Eigene faellige Popup-Erinnerungen; reines Lesen stellt keine Benachrichtigung zu. reference fuer Folgeaktionen verwenden. Abschliessen beendet auch die zugehoerige Aktivitaet."}


def mutate_reminders(service, values, *, action):
    user, _ = require_actor(service, "activities", "edit")
    allowed = {"notification_ids", "reminder_refs"} | ({"minutes"} if action == "snooze" else {"minutes_before"} if action == "snooze_before_start" else set())
    if set(values) - allowed or any(not isinstance(v, str) or len(v) > 12000 for v in values.values()):
        raise HubOperationError("Ungueltige Erinnerungseingaben.")
    if bool(values.get("notification_ids")) == bool(values.get("reminder_refs")):
        raise HubOperationError("Genau eine Erinnerungsauswahl angeben.")
    try:
        selected = json.loads(values.get("notification_ids") or values["reminder_refs"])
    except (ValueError, KeyError) as exc:
        raise HubOperationError("Ungueltige Erinnerungsauswahl.") from exc
    by_reference = bool(values.get("reminder_refs"))
    if not isinstance(selected, list) or not 1 <= len(selected) <= 100 or any(type(item) is not (str if by_reference else int) for item in selected):
        raise HubOperationError("Bitte 1 bis 100 gueltige Erinnerungen auswaehlen.")
    domain = CustomerDesktopReminderService(db=service.db)
    # Selecting and materializing virtual reminders belongs to the confirmed mutation, never a query.
    with service.db.begin_nested():
        ids = selected
        if by_reference:
            candidates = domain._materialize_due_popup_reminders(user=user, now=domain._utc_now(), persist=False)
            existing = service.db.scalars(select(CustomerActivityReminderNotification).where(
                CustomerActivityReminderNotification.user_id == user.id,
                CustomerActivityReminderNotification.completed_at.is_(None))).all()
            by_key = {f"{row.activity_kind}:{row.activity_id}:{row.reminder_key}": row for row in (*candidates, *existing)}
            if not set(selected) <= by_key.keys():
                raise HubOperationError("Eine Erinnerung ist nicht mehr verfuegbar. Bitte erneut lesen.")
            rows = [by_key[key] for key in dict.fromkeys(selected)]
            for row in rows:
                if row.id is None:
                    service.db.add(row)
            service.db.flush()
            ids = [row.id for row in rows]
        try:
            if action == "snooze":
                return domain.snooze_reminders(user=user, notification_ids=ids, minutes=int(values.get("minutes", "")))
            if action == "snooze_before_start":
                return domain.snooze_reminders_before_start(user=user, notification_ids=ids, minutes_before=int(values.get("minutes_before", "")))
            return domain.complete_reminders(user=user, notification_ids=ids)
        except (ValueError, OverflowError) as exc:
            raise HubOperationError(str(exc)) from exc


def execute(service, values, *, action):
    changed = mutate_reminders(service, values, action=action)
    return HubOperationResult("Erinnerungen aktualisiert", "/calendar", 0, outputs={"changed": str(changed)})


register_query(HubQuery("reminders.list", "Eigene faellige Desktop-Erinnerungen ohne Zustellung oder Aenderung lesen. Aktuelle Aktivitaets- und Datensatzrechte gelten.", (Field("offset", "Listenbeginn"),), read_reminders))
for action, extra in (("complete", ()), ("snooze", (Field("minutes", "Erneut erinnern nach Minuten", required=True, options=tuple((str(n), str(n)) for n in SNOOZE_MINUTES_OPTIONS)),)),
                      ("snooze_before_start", (Field("minutes_before", "Minuten vor Start", required=True, options=(("0", "Zum Start"), ("5", "5 Minuten vorher"))),))):
    register_operation(HubOperation(key=f"reminders.{action}", module="activities", label=f"Erinnerungen: {action}",
        description="Eigene Erinnerungen nach Bestaetigung verschieben oder einschliesslich Aktivitaet abschliessen. Nutzt dieselbe Funktion wie der Desktop-Client.",
        input_guide="reminders.list lesen; reminder_refs als JSON-Liste der reference-Werte angeben. Alternativ notification_ids als JSON-Liste bereits zugestellter IDs, nie beides. complete schliesst auch die Aktivitaet ab.",
        input_fields=lambda extra=extra: (Field("reminder_refs", "Erinnerungsverweise als JSON-Liste", max_length=12000), Field("notification_ids", "Benachrichtigungs-IDs als JSON-Liste", max_length=12000), *extra),
        preview_fields=(("reminder_refs", "Erinnerungen"), ("notification_ids", "Erinnerungen"), ("minutes", "Minuten"), ("minutes_before", "Minuten vor Beginn")),
        execute=partial(execute, action=action), result_fields=(("changed", "Anzahl geaenderter Erinnerungen"),)))
