from typing import Annotated, Literal
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.security import get_secret_cipher
from app.services.hub_operations import HubOperationService, HubOperationError
from app.services.hub_operation_desktop_reminders import reminder_views, mutate_reminders
from app.services.audit import write_audit_log
from app.services.customer_desktop_reminders import (
    SNOOZE_MINUTES_OPTIONS,
    CustomerDesktopReminderService,
    DesktopReminderError,
)


router = APIRouter(prefix="/api/v1/desktop", tags=["desktop-notifications"])


class ReminderSelection(BaseModel):
    notification_ids: list[int] = Field(min_length=1, max_length=100)


class SnoozeSelection(ReminderSelection):
    minutes: int


class SnoozeBeforeStartSelection(ReminderSelection):
    minutes_before: Literal[0, 5]


@router.get("/reminders")
def list_desktop_reminders(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _desktop_user_or_error(request)
    try:
        reminders = reminder_views(_reminder_gateway(db, user), deliver=True)
    except HubOperationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    db.commit()
    return {
        "reminders": [reminder.as_dict() for reminder in reminders],
        "snooze_minutes_options": SNOOZE_MINUTES_OPTIONS,
    }


@router.post("/reminders/snooze")
def snooze_desktop_reminders(
    payload: SnoozeSelection,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _desktop_user_or_error(request)
    try:
        changed = mutate_reminders(_reminder_gateway(db, user), {
            "notification_ids": json.dumps(payload.notification_ids), "minutes": str(payload.minutes)}, action="snooze")
    except (DesktopReminderError, HubOperationError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-desktop-notifier",
        action="snooze-customer-activity-reminders",
        result="ok",
        detail=f"Snoozed {changed} customer activity reminders for {payload.minutes} minutes.",
    )
    db.commit()
    return {"snoozed": changed}


@router.post("/reminders/snooze-before-start")
def snooze_desktop_reminders_before_start(
    payload: SnoozeBeforeStartSelection,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _desktop_user_or_error(request)
    try:
        changed = mutate_reminders(_reminder_gateway(db, user), {
            "notification_ids": json.dumps(payload.notification_ids), "minutes_before": str(payload.minutes_before)}, action="snooze_before_start")
    except (DesktopReminderError, HubOperationError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-desktop-notifier",
        action="snooze-customer-activity-reminders-before-start",
        result="ok",
        detail=f"Snoozed {changed} customer activity reminders until {payload.minutes_before} minutes before their starts.",
    )
    db.commit()
    return {"snoozed": changed}


@router.post("/reminders/complete")
def complete_desktop_reminders(
    payload: ReminderSelection,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _desktop_user_or_error(request)
    try:
        changed = mutate_reminders(_reminder_gateway(db, user), {
            "notification_ids": json.dumps(payload.notification_ids)}, action="complete")
    except (DesktopReminderError, HubOperationError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-desktop-notifier",
        action="complete-customer-activity-reminders",
        result="ok",
        detail=f"Completed {changed} customer activity reminders.",
    )
    db.commit()
    return {"completed": changed}


def _reminder_gateway(db, user):
    return HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username)


def _desktop_user_or_error(request: Request):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Desktop-Gerät muss angemeldet sein.")
    return user
