"""Fixed Hub workflows and their related records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.customer_activity import CustomerTaskActivity
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.hub_case import HubCase
from app.models.hub_workflow import HubWorkflow
from app.services.task_email_reminders import TaskEmailReminderService


CASE_OPEN_REMINDER_WORKFLOW_KEY = "case-open-popup-reminder"
LEAD_RESULT_FIELD_UPDATE_WORKFLOW_KEY = "lead-result-field-updates"
LEAD_APPOINTMENT_REMINDER_WORKFLOW_KEY = "lead-appointment-reminder"
# This Hub-managed template remains stable even if its display name changes later.
CASE_COMPLETION_EMAIL_TEMPLATE_ID = "hub-template-d183cf19fa8b41a78d32892037fe8f39"
_BERLIN = ZoneInfo("Europe/Berlin")
_CASE_OPEN_REMINDER_DESCRIPTION = (
    "Bei jedem neu angelegten, noch offenen Fall erstellt der Hub automatisch eine Aufgabe "
    "mit Popup-Erinnerung. Die Aufgabe wird 20 Stunden nach dem Erstellungszeitpunkt des Falls "
    "fällig und heißt \"Ein offener Fall vom <Erstellungszeitpunkt>\". Wird der Fall abgeschlossen "
    "oder gelöscht, entfernt der Hub die zugehörige Aufgabe einschließlich noch offener Popup-Erinnerungen. "
    "Beim ersten Speichern mit Status \"Abgeschlossen\" öffnet der Hub außerdem eine neue E-Mail mit "
    "der fest hinterlegten Vorlage \"Nach Erledigung Änderungswunsch hub\"."
)
_LEAD_RESULT_FIELD_UPDATE_DESCRIPTION = (
    "Setzt bei Änderungen am Lead-Ergebnis automatisch den passenden Lead-Status und, "
    "sofern vorgesehen, das Abrechnungsergebnis."
)
_LEAD_APPOINTMENT_REMINDER_DESCRIPTION = (
    "Setzt die Termin-Erinnerung bei Beratungsterminen mit mehr als 60 Stunden Vorlauf und "
    "entfernt sie bei kürzerem Vorlauf oder Stornierung."
)
_LEAD_RESULT_FIELD_UPDATES: dict[str, dict[str, str]] = {
    "Stattgefunden + Auftrag": {
        "lead_status": "Umgewandelt",
        "billing_result": "Stattgefunden + Auftrag",
    },
    "Storniert": {
        "lead_status": "Junk Lead",
        "billing_result": "Storno",
    },
    "Zukünftig kontaktieren": {"lead_status": "Contact in Future"},
    "Kein Auftrag": {
        "lead_status": "Lost Lead",
        "billing_result": "Stattgefunden",
    },
    "Stattgefunden und kein Auftrag": {
        "lead_status": "Lost Lead",
        "billing_result": "Stattgefunden",
    },
    "Stattgefunden": {
        "lead_status": "Contacted",
        "billing_result": "Stattgefunden",
    },
    "Vertrag": {
        "lead_status": "Umgewandelt",
        "billing_result": "Auftrag",
    },
    "Termin muss neugelegt werden": {
        "lead_status": "Storno",
        "billing_result": "Storno",
    },
    "Termin Beratungsgespräch": {"lead_status": "Pre Qualified"},
    "Nicht mehr kontaktieren": {"lead_status": "Nicht mehr kontaktieren"},
    "Rücktritt": {"lead_status": "Rücktritt"},
}
_LEAD_APPOINTMENT_REMINDER_CANCEL_RESULTS = {
    "Storniert",
    "Termin muss neugelegt werden",
    "Termin muss neu gelegt werden",
}


class HubWorkflowService:
    """Keep fixed workflow definitions and their side effects in one place."""

    def __init__(self, *, db: Session):
        self.db = db

    def ensure_default_workflows(self) -> None:
        definitions = (
            (
                CASE_OPEN_REMINDER_WORKFLOW_KEY,
                "Offenen Fall nachfassen",
                "Fälle · Neuer Fall, Abschluss",
                _CASE_OPEN_REMINDER_DESCRIPTION,
            ),
            (
                LEAD_RESULT_FIELD_UPDATE_WORKFLOW_KEY,
                "Lead-Ergebnis Folgefelder",
                "Leads · Änderung Lead-Ergebnis",
                _LEAD_RESULT_FIELD_UPDATE_DESCRIPTION,
            ),
            (
                LEAD_APPOINTMENT_REMINDER_WORKFLOW_KEY,
                "Beratungstermin-Erinnerung",
                "Leads · Termindatum, Lead-Ergebnis",
                _LEAD_APPOINTMENT_REMINDER_DESCRIPTION,
            ),
        )
        for workflow_key, title, module_label, description in definitions:
            workflow = self.db.scalar(
                select(HubWorkflow).where(HubWorkflow.workflow_key == workflow_key)
            )
            if workflow is None:
                self.db.add(
                    HubWorkflow(
                        workflow_key=workflow_key,
                        title=title,
                        module_label=module_label,
                        description=description,
                    )
                )
                continue
            workflow.module_label = module_label
            workflow.description = description
        self.db.flush()

    def list_workflows(self) -> tuple[HubWorkflow, ...]:
        self.ensure_default_workflows()
        return tuple(
            self.db.scalars(
                select(HubWorkflow).order_by(HubWorkflow.created_at.asc(), HubWorkflow.id.asc())
            ).all()
        )

    def apply_lead_field_updates(
        self,
        *,
        previous_values: dict[str, object],
        updated_values: dict[str, object],
        now: datetime | None = None,
    ) -> bool:
        """Apply all enabled workflows that derive one Lead field from others."""
        self.ensure_default_workflows()
        workflows = {
            workflow.workflow_key: workflow
            for workflow in self.db.scalars(
                select(HubWorkflow).where(
                    HubWorkflow.workflow_key.in_(
                        (
                            LEAD_RESULT_FIELD_UPDATE_WORKFLOW_KEY,
                            LEAD_APPOINTMENT_REMINDER_WORKFLOW_KEY,
                        )
                    )
                )
            ).all()
        }
        changed = False

        previous_result = previous_values.get("lead_result")
        updated_result = updated_values.get("lead_result")
        field_updates = _LEAD_RESULT_FIELD_UPDATES.get(updated_result)
        result_workflow = workflows.get(LEAD_RESULT_FIELD_UPDATE_WORKFLOW_KEY)
        if (
            isinstance(updated_result, str)
            and updated_result != previous_result
            and result_workflow is not None
            and result_workflow.is_enabled
            and field_updates is not None
        ):
            updated_values.update(field_updates)
            changed = True

        reminder_workflow = workflows.get(LEAD_APPOINTMENT_REMINDER_WORKFLOW_KEY)
        if reminder_workflow is None or not reminder_workflow.is_enabled:
            return changed

        reminder_enabled = self._should_enable_lead_appointment_reminder(
            appointment_at=updated_values.get("appointment_at"),
            lead_result=updated_values.get("lead_result"),
            now=now,
        )
        if updated_values.get("appointment_reminder") != reminder_enabled:
            updated_values["appointment_reminder"] = reminder_enabled
            changed = True

        return changed

    @staticmethod
    def _should_enable_lead_appointment_reminder(
        *,
        appointment_at: object,
        lead_result: object,
        now: datetime | None,
    ) -> bool:
        if lead_result in _LEAD_APPOINTMENT_REMINDER_CANCEL_RESULTS:
            return False
        if not isinstance(appointment_at, str) or not appointment_at.strip():
            return False
        try:
            appointment = datetime.fromisoformat(appointment_at.strip().replace("Z", "+00:00"))
        except ValueError:
            return False
        if appointment.tzinfo is None:
            appointment = appointment.replace(tzinfo=_BERLIN)
        else:
            appointment = appointment.astimezone(_BERLIN)

        current_time = now or datetime.now(_BERLIN)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=_BERLIN)
        else:
            current_time = current_time.astimezone(_BERLIN)
        return appointment - current_time > timedelta(hours=60)

    def create_case_open_reminder(
        self,
        *,
        case: HubCase,
        case_status: str,
        created_time: str,
        actor_username: str | None,
    ) -> CustomerTaskActivity | None:
        """Create the one popup task for an open, customer-linked Hub case."""
        self.ensure_default_workflows()
        workflow = self.db.scalar(
            select(HubWorkflow).where(HubWorkflow.workflow_key == CASE_OPEN_REMINDER_WORKFLOW_KEY)
        )
        if workflow is None or not workflow.is_enabled or case_status == "Abgeschlossen":
            return None
        existing = self.db.scalar(
            select(CustomerTaskActivity).where(CustomerTaskActivity.case_id == case.id)
        )
        if existing is not None:
            return existing

        case_created_at = self._parse_case_created_at(created_time)
        created_display = case_created_at.astimezone(_BERLIN).strftime("%d.%m.%Y %H:%M")
        from app.services.hub_activity_responsibility import initial_responsibility
        task = CustomerTaskActivity(
            customer_id=case.customer_id,
            case_id=case.id,
            name=f"Ein offener Fall vom {created_display}",
            status="planned",
            due_at=(case_created_at + timedelta(hours=20)).astimezone(UTC).replace(tzinfo=None),
            reminder_channel="popup",
            reminder_minutes_before=0,
            description="Automatisch durch den Workflow \"Offenen Fall nachfassen\" erstellt.",
            created_by_username=(actor_username or "system").strip() or "system",
            **initial_responsibility(self.db, (actor_username or "system").strip() or "system"),
        )
        self.db.add(task)
        self.db.flush()
        return task

    def remove_case_open_reminder(self, *, case_id: int) -> int:
        """Delete workflow tasks and their pending popup notifications for one case."""
        tasks = list(
            self.db.scalars(
                select(CustomerTaskActivity).where(CustomerTaskActivity.case_id == case_id)
            ).all()
        )
        for task in tasks:
            TaskEmailReminderService(db=self.db).cancel_for_deleted_task(task=task)
            self.db.execute(
                delete(CustomerActivityReminderNotification).where(
                    CustomerActivityReminderNotification.activity_kind == "task",
                    CustomerActivityReminderNotification.activity_id == task.id,
                )
            )
            self.db.delete(task)
        self.db.flush()
        return len(tasks)

    @staticmethod
    def _parse_case_created_at(value: str) -> datetime:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=_BERLIN)
        except ValueError:
            return datetime.now(_BERLIN).replace(second=0, microsecond=0)
