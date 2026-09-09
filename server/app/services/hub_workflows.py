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
_BERLIN = ZoneInfo("Europe/Berlin")
_CASE_OPEN_REMINDER_DESCRIPTION = (
    "Bei jedem neu angelegten, noch offenen Fall erstellt der Hub automatisch eine Aufgabe "
    "mit Popup-Erinnerung. Die Aufgabe wird 20 Stunden nach dem Erstellungszeitpunkt des Falls "
    "fällig und heißt \"Ein offener Fall vom <Erstellungszeitpunkt>\". Wird der Fall abgeschlossen "
    "oder gelöscht, entfernt der Hub die zugehörige Aufgabe einschließlich noch offener Popup-Erinnerungen."
)


class HubWorkflowService:
    """Keep fixed workflow definitions and their side effects in one place."""

    def __init__(self, *, db: Session):
        self.db = db

    def ensure_default_workflows(self) -> None:
        workflow = self.db.scalar(
            select(HubWorkflow).where(HubWorkflow.workflow_key == CASE_OPEN_REMINDER_WORKFLOW_KEY)
        )
        if workflow is not None:
            return
        self.db.add(
            HubWorkflow(
                workflow_key=CASE_OPEN_REMINDER_WORKFLOW_KEY,
                title="Offenen Fall nachfassen",
                module_label="Fälle · Neuer Fall",
                description=_CASE_OPEN_REMINDER_DESCRIPTION,
            )
        )
        self.db.flush()

    def list_workflows(self) -> tuple[HubWorkflow, ...]:
        self.ensure_default_workflows()
        return tuple(
            self.db.scalars(
                select(HubWorkflow).order_by(HubWorkflow.created_at.asc(), HubWorkflow.id.asc())
            ).all()
        )

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
