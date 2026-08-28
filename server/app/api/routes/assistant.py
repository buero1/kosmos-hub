from pathlib import Path
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.routes.accounts import get_csrf_token, require_csrf
from app.core.security import get_secret_cipher
from app.core.templates import create_templates
from app.db.session import get_db
from app.services.ai_assistant import AssistantError, HubAssistantService
from app.services.ai_provider import AiProviderConfigService
from app.services.audit import write_audit_log
from app.services.maintenance_runs import MaintenanceRunService
from app.services.maintenance_worker import schedule_pending_direct_updates

templates = create_templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(prefix="/assistant", include_in_schema=False)
_MIN_REQUEST_INTERVAL_SECONDS = 3.0


@router.get("", response_class=HTMLResponse)
def assistant_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = _current_user(request)
    provider = AiProviderConfigService(db=db, cipher=get_secret_cipher()).get_openai_config()
    return templates.TemplateResponse(
        request,
        "assistant.html",
        _page_context(request, provider_configured=provider is not None and provider.enabled, user=user),
    )


@router.post("", response_class=HTMLResponse)
def ask_assistant(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    question: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _current_user(request)
    provider = AiProviderConfigService(db=db, cipher=get_secret_cipher()).get_openai_config()
    context = _page_context(request, provider_configured=provider is not None and provider.enabled, user=user, question=question)
    if provider is None or not provider.enabled:
        context["error"] = "Connect OpenAI in Account before using the assistant."
        return templates.TemplateResponse(request, "assistant.html", context, status_code=400)

    try:
        _require_request_interval(request)
        answer = HubAssistantService(db=db, cipher=get_secret_cipher()).answer(
            question,
            previous_site_ids=_assistant_previous_site_ids(request),
        )
    except (AssistantError, ValueError) as exc:
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-assistant",
            action="assistant-question",
            result="error",
            detail="The assistant request failed. Question content is intentionally not stored.",
        )
        db.commit()
        context["error"] = str(exc)
        return templates.TemplateResponse(request, "assistant.html", context, status_code=400)

    if answer.update_matches:
        request.session["assistant_previous_site_ids"] = list(
            dict.fromkeys(match.site_id for match in answer.update_matches)
        )

    if answer.action is not None:
        try:
            outcome = MaintenanceRunService(db=db, cipher=get_secret_cipher()).start_direct_updates(
                selected_keys=list(answer.action.selected_keys),
                actor=user.username,
            )
        except ValueError as exc:
            answer = answer.with_action_error(str(exc))
            action_result = "error"
            action_detail = "The assistant could not queue the validated direct updates."
        else:
            schedule_pending_direct_updates()
            answer = answer.with_queued_action(batch_id=outcome.batch_id, message=outcome.message)
            action_result = "queued"
            action_detail = (
                f"The assistant queued {outcome.run_count} validated direct plugin update run(s) "
                f"in batch {outcome.batch_id[:12]}."
            )
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-assistant",
            action="assistant-plugin-update-command",
            result=action_result,
            detail=action_detail,
            request_id=answer.action.batch_id,
        )
    else:
        action_detail = "Assistant answer generated. Question content is intentionally not stored."

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-assistant",
        action="assistant-question",
        result="success",
        detail=action_detail,
    )
    db.commit()
    context.update({"answer": answer, "question": ""})
    return templates.TemplateResponse(request, "assistant.html", context)


def _page_context(request: Request, *, provider_configured: bool, user, question: str = "") -> dict:
    return {
        "csrf_token": get_csrf_token(request),
        "provider_configured": provider_configured,
        "question": question,
        "user": user,
    }


def _current_user(request: Request):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _require_request_interval(request: Request) -> None:
    now = monotonic()
    previous = request.session.get("assistant_request_at")
    if isinstance(previous, (int, float)) and now - previous < _MIN_REQUEST_INTERVAL_SECONDS:
        raise AssistantError("Please wait a few seconds before asking the next question.")
    request.session["assistant_request_at"] = now


def _assistant_previous_site_ids(request: Request) -> tuple[int, ...]:
    values = request.session.get("assistant_previous_site_ids", [])
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, int) and value > 0)
