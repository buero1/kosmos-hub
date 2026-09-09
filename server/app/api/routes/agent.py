"""User-facing, explicitly approved Hub agent actions."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.csrf import get_csrf_token, require_csrf
from app.core.security import get_secret_cipher
from app.core.templates import create_templates
from app.db.session import get_db
from app.services.ai_provider import AiProviderConfigService
from app.services.audit import write_audit_log
from app.services.hub_agent import HubAgentError, HubAgentService

templates = create_templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(prefix="/agent", include_in_schema=False)


@router.get("", response_class=HTMLResponse)
def agent_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email_key: Annotated[str, Query()] = "",
):
    user = _current_user(request)
    return templates.TemplateResponse(
        request,
        "agent.html",
        _page_context(request, db=db, user=user, email_key=email_key),
    )


@router.post("", response_class=HTMLResponse)
def plan_agent_work(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    instruction: Annotated[str, Form()] = "",
    email_key: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        job = service.plan(instruction=instruction, actor=user.username, email_key=email_key)
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-agent",
            action="agent-plan-created",
            result="success",
            detail=f"Created Hub agent job {job.id} with {len(job.actions)} proposed actions. Request content is encrypted and omitted from the audit log.",
        )
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        context = _page_context(request, db=db, user=user, instruction=instruction, email_key=email_key)
        context["error"] = str(exc)
        return templates.TemplateResponse(request, "agent.html", context, status_code=400)
    suffix = f"&email_key={email_key}" if email_key.strip() else ""
    return RedirectResponse(url=f"/agent?agent_job={job.id}{suffix}", status_code=303)


@router.post("/actions/{action_id}/execute")
def execute_agent_action(
    action_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        action = service.execute_action(action_id=action_id, actor=user.username)
        result = "success" if action.status == "completed" else "error"
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-agent",
            action=f"agent-{action.action_type}",
            result=result,
            detail=f"Hub agent action {action.id} finished with status {action.status}. Sensitive action data is encrypted and omitted from the audit log.",
        )
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        return RedirectResponse(url="/agent?agent_action=missing", status_code=303)
    outcome = "completed" if action.status == "completed" else "failed"
    return RedirectResponse(url=f"/agent?agent_action={outcome}", status_code=303)


def _page_context(request: Request, *, db: Session, user, instruction: str = "", email_key: str = "") -> dict:
    provider = AiProviderConfigService(db=db, cipher=get_secret_cipher()).get_openai_config()
    agent = HubAgentService(db=db, cipher=get_secret_cipher())
    email_context = None
    context_error = None
    if email_key.strip():
        try:
            email_context = agent.get_email_context(email_key=email_key)
        except HubAgentError as exc:
            context_error = str(exc)
    return {
        "csrf_token": get_csrf_token(request),
        "user": user,
        "instruction": instruction,
        "email_context": email_context,
        "email_context_error": context_error,
        "provider_configured": provider is not None and provider.enabled,
        "capabilities": HubAgentService.capabilities(),
        "jobs": agent.list_jobs(actor=user.username),
    }


def _current_user(request: Request):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user
