from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import get_secret_cipher
from app.db.session import get_db
from app.models.customer_activity import CustomerCallActivity, CustomerMeetingActivity
from app.services.audit import write_audit_log
from app.services.customer_activities import CustomerActivityError, CustomerActivityService
from app.services.hub_lead_notes import HubLeadNoteError, HubLeadNoteService
from app.services.hub_leads import HubLeadError, HubLeadService


router = APIRouter(prefix="/api/v1/integrations/callapp", tags=["callapp-integration"])


class CallAppLeadPayload(BaseModel):
    id: str = Field(min_length=1, max_length=255)
    first_name: str = Field(default="", max_length=255)
    last_name: str = Field(default="", max_length=255)
    company: str = Field(default="", max_length=500)
    email: str = Field(default="", max_length=500)
    phone: str = Field(default="", max_length=255)
    website: str = Field(default="", max_length=2000)
    street: str = Field(default="", max_length=1000)
    postal_code: str = Field(default="", max_length=255)
    city: str = Field(default="", max_length=255)
    country: str = Field(default="", max_length=255)
    industry: str = Field(default="", max_length=1000)
    homepage_state: str = Field(default="", max_length=1000)


class CallAppCampaignPayload(BaseModel):
    id: str = Field(min_length=1, max_length=255)
    name: str = Field(default="", max_length=500)
    source: str = Field(default="CallApp", max_length=255)


class CallAppCallPayload(BaseModel):
    id: str = Field(min_length=1, max_length=255)
    started_at: datetime
    duration_seconds: int = Field(default=0, ge=0, le=86_400)
    direction: Literal["outbound", "inbound"] = "outbound"
    assistant: str = Field(default="", max_length=255)
    recording_url: HttpUrl | None = None
    transcript_url: HttpUrl | None = None


class CallAppFollowUpPayload(BaseModel):
    starts_at: datetime
    subject: str = Field(default="Follow-up Anruf", max_length=255)


class CallAppClosurePayload(BaseModel):
    instance_id: str = Field(default="primary", min_length=1, max_length=48, pattern=r"^[A-Za-z0-9._-]+$")
    closure_id: str = Field(min_length=1, max_length=255)
    occurred_at: datetime
    result: str = Field(default="Lead erstellt", max_length=1000)
    notes: str = Field(default="", max_length=30_000)
    manual_note: str = Field(default="", max_length=20_000)
    lead: CallAppLeadPayload
    campaign: CallAppCampaignPayload
    call: CallAppCallPayload | None = None
    follow_up: CallAppFollowUpPayload | None = None


def _integration_actor(request: Request) -> tuple[str, object]:
    user = getattr(request.state, "hub_user", None)
    token = getattr(request.state, "integration_token", None)
    if user is None or token is None or getattr(token, "source_key", "") != "callapp":
        raise HTTPException(status_code=401, detail="Gültige CallApp-Verbindung erforderlich.")
    return f"integration:callapp:{token.id}", user


def _source_system(payload: CallAppClosurePayload) -> str:
    return f"callapp:{payload.instance_id}"[:96]


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


@router.get("/health")
def integration_health(request: Request):
    actor, _user = _integration_actor(request)
    return {"ok": True, "integration": "callapp", "actor": actor}


@router.post("/closures")
def receive_closure(
    payload: CallAppClosurePayload,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    actor, user = _integration_actor(request)
    source_system = _source_system(payload)
    fields: dict[str, object] = {
        "first_name": payload.lead.first_name,
        "last_name": payload.lead.last_name,
        "company": payload.lead.company,
        "email": payload.lead.email,
        "phone": payload.lead.phone,
        "website": payload.lead.website,
        "street": payload.lead.street,
        "postal_code": payload.lead.postal_code,
        "city": payload.lead.city,
        "country": payload.lead.country,
        "industry": payload.lead.industry,
        "homepage": payload.lead.homepage_state,
        "dialfire_campaign_name": payload.campaign.name,
        "source": payload.campaign.source or "CallApp",
        "lead_status": "Lead erstellt",
        "lead_type": "Angebot vereinbart",
        "lead_result": "Offen",
        "condition": "Option 1",
        "created_at_source": payload.occurred_at.isoformat(),
        "dialfire_comment": payload.notes,
    }
    try:
        lead, created = HubLeadService(db=db, cipher=get_secret_cipher()).upsert_external_lead(
            source_system=source_system,
            source_external_id=payload.lead.id,
            field_values=fields,
        )
        note_id = None
        note_sections = [
            f"{label}:\n{text.strip()}"
            for label, text in (("System Kommentar", payload.notes), ("Manuelle Notiz", payload.manual_note))
            if text.strip()
        ]
        if note_sections:
            note = HubLeadNoteService(db=db, cipher=get_secret_cipher()).upsert_external_note(
                lead_id=lead.id,
                source_system=source_system,
                source_external_id=payload.closure_id,
                actor=actor,
                title="Gesprächsnotiz",
                content="\n\n".join(note_sections),
            )
            note_id = note.id

        # Legacy clients may still supply a call, but only the follow-up is imported.
        activities = CustomerActivityService(db=db)

        follow_up_id = None
        if payload.follow_up is not None:
            follow_up = activities.upsert_external_call(
                lead_id=lead.id,
                source_system=source_system,
                source_external_id=f"{payload.closure_id}:follow-up",
                actor=actor,
                name=payload.follow_up.subject,
                status="planned",
                direction="outbound",
                starts_at=payload.follow_up.starts_at,
                duration_seconds=30 * 60,
                description=payload.manual_note.strip() or f"Wiedervorlage aus CallApp · Kampagne: {payload.campaign.name}".strip(),
            )
            follow_up_id = follow_up.id
    except (HubLeadError, HubLeadNoteError, CustomerActivityError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    write_audit_log(
        db,
        site=None,
        actor=actor,
        source="callapp-integration",
        action="upsert-callapp-closure",
        result="ok",
        detail=f"Upserted CallApp closure {payload.closure_id} for Hub Lead {lead.id}; personal data is not retained in the audit log.",
    )
    db.commit()
    base_url = get_settings().public_base_url.rstrip("/")
    return {
        "ok": True,
        "created": created,
        "lead_id": str(lead.id),
        "lead_url": f"{base_url}/leads/{lead.id}",
        "note_id": str(note_id) if note_id is not None else None,
        "call_id": None,
        "follow_up_id": str(follow_up_id) if follow_up_id is not None else None,
    }


@router.get("/busy-times")
def list_busy_times(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
):
    _integration_actor(request)
    start_value = _utc_naive(start)
    end_value = _utc_naive(end)
    if end_value <= start_value or end_value - start_value > timedelta(days=120):
        raise HTTPException(status_code=400, detail="Ungültiger Zeitraum.")
    calls = db.scalars(
        select(CustomerCallActivity).where(
            CustomerCallActivity.status == "planned",
            CustomerCallActivity.starts_at < end_value,
            CustomerCallActivity.ends_at > start_value,
        )
    ).all()
    meetings = db.scalars(
        select(CustomerMeetingActivity).where(
            CustomerMeetingActivity.status == "planned",
            CustomerMeetingActivity.starts_at.is_not(None),
            CustomerMeetingActivity.ends_at.is_not(None),
            CustomerMeetingActivity.starts_at < end_value,
            CustomerMeetingActivity.ends_at > start_value,
        )
    ).all()
    entries = [
        {"kind": "call", "id": str(item.id), "start": item.starts_at.replace(tzinfo=UTC).isoformat(), "end": item.ends_at.replace(tzinfo=UTC).isoformat(), "title": item.name}
        for item in calls
    ] + [
        {"kind": "meeting", "id": str(item.id), "start": item.starts_at.replace(tzinfo=UTC).isoformat(), "end": item.ends_at.replace(tzinfo=UTC).isoformat(), "title": item.name}
        for item in meetings
        if item.starts_at is not None and item.ends_at is not None
    ]
    entries.sort(key=lambda item: (item["start"], item["kind"], item["id"]))
    return {"busy_times": entries}
