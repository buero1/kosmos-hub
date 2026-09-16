"""Lead-owned calls, tasks and meetings using the shared activity validation."""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.customer_activity import CustomerCallActivity, CustomerMeetingActivity, CustomerTaskActivity
from app.services.customer_activities import CustomerActivityError, CustomerActivityService
from app.services.task_email_reminders import TaskEmailReminderService


class LeadActivityService:
    def __init__(self, *, db: Session):
        self.db = db
        self.activities = CustomerActivityService(db=db)

    def update_call(self, *, lead_id: int, call_id: int, **values) -> CustomerCallActivity:
        self._owned(CustomerCallActivity, lead_id=lead_id, activity_id=call_id)
        return self.activities.update_calendar_call(call_id=call_id, customer_id=None, **values)

    def update_meeting(self, *, lead_id: int, meeting_id: int, **values) -> CustomerMeetingActivity:
        self._owned(CustomerMeetingActivity, lead_id=lead_id, activity_id=meeting_id)
        return self.activities.update_calendar_meeting(meeting_id=meeting_id, customer_id=None, **values)

    def update_task(self, *, lead_id: int, task_id: int, **values) -> CustomerTaskActivity:
        task = self._owned(CustomerTaskActivity, lead_id=lead_id, activity_id=task_id)
        validated = self.activities._validated_task_values(**values)
        for key, value in validated.items():
            setattr(task, key, value)
        self.db.flush()
        self.activities._sync_task_email_reminder(task)
        return task

    def delete(self, *, lead_id: int, kind: str, activity_id: int):
        activity = self._owned_kind(lead_id=lead_id, kind=kind, activity_id=activity_id)
        if isinstance(activity, CustomerTaskActivity):
            TaskEmailReminderService(db=self.db).cancel_for_deleted_task(task=activity)
        self.db.delete(activity)
        self.db.flush()
        return activity

    def complete(self, *, lead_id: int, kind: str, activity_id: int):
        if kind not in {"call", "task"}:
            raise CustomerActivityError("Diese Aktivität kann nicht abgeschlossen werden.")
        activity = self._owned_kind(lead_id=lead_id, kind=kind, activity_id=activity_id)
        if activity.status == "planned":
            activity.status = "completed"
            self.db.flush()
            if isinstance(activity, CustomerTaskActivity):
                self.activities._sync_task_email_reminder(activity)
        return activity

    def _owned_kind(self, *, lead_id: int, kind: str, activity_id: int):
        models = {
            "call": CustomerCallActivity,
            "task": CustomerTaskActivity,
            "meeting": CustomerMeetingActivity,
        }
        model = models.get(kind)
        if model is None:
            raise CustomerActivityError("Die Aktivität wurde nicht gefunden.")
        return self._owned(model, lead_id=lead_id, activity_id=activity_id)

    def _owned(self, model, *, lead_id: int, activity_id: int):
        query = select(model).where(model.id == activity_id, model.lead_id == lead_id)
        if model is CustomerCallActivity or model is CustomerMeetingActivity:
            query = query.options(selectinload(model.reminders))
        activity = self.db.scalar(query)
        if activity is None:
            raise CustomerActivityError("Die Aktivität wurde nicht gefunden.")
        return activity
