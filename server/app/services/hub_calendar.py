"""Calendar projections shared by the UI, integration and agent, without writes."""
from datetime import UTC, datetime, timedelta
from sqlalchemy import select

from app.services.customer_activities import CustomerActivityService
from app.services.hub_operation_activities import ACTIVITY_MODELS, _can_access
from app.services.hub_operations import HubOperationError
from app.services.hub_record_access import require_actor
from app.services.hub_activity_responsibility import ActivityResponsibility


def calendar_activities(service, week_start, *, view="all"):
    user, access = require_actor(service, "activities", "view")
    policy = ActivityResponsibility(service.db, user)
    allowed = {kind: {row.id for row in service.db.scalars(select(ACTIVITY_MODELS[kind]))
                     if policy.matches(row, view)} for kind in ("call", "meeting")}
    entries = CustomerActivityService(db=service.db).list_calendar_activities(
        week_start=week_start, allowed_call_ids=allowed["call"], allowed_meeting_ids=allowed["meeting"], persist_elapsed=False)
    from dataclasses import replace
    return tuple(replace(entry, **policy.flags(service.db.get(ACTIVITY_MODELS[entry.kind], entry.id))) for entry in entries)


def busy_times(service, start, end):
    user, access = require_actor(service, "activities", "view")
    start = start.astimezone(UTC).replace(tzinfo=None) if start.tzinfo else start
    end = end.astimezone(UTC).replace(tzinfo=None) if end.tzinfo else end
    if end <= start or end - start > timedelta(days=120):
        raise HubOperationError("Ungueltiger Zeitraum (maximal 120 Tage).")
    entries = []
    policy = ActivityResponsibility(service.db, user)
    for kind in ("call", "meeting"):
        model = ACTIVITY_MODELS[kind]
        for row in service.db.scalars(select(model).where(model.status == "planned", model.starts_at < end, model.ends_at > start)):
            if policy.visible(row):
                entries.append({"kind": kind, "id": str(row.id), "start": row.starts_at.replace(tzinfo=UTC).isoformat(),
                    "end": row.ends_at.replace(tzinfo=UTC).isoformat(), "title": row.name})
    return sorted(entries, key=lambda row: (row["start"], row["kind"], row["id"]))
