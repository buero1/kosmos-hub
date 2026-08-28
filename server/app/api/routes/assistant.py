from pathlib import Path
from time import monotonic
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
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
from app.services.fleet_inventory import FleetInventoryService
from app.services.site_selection import build_site_selector_context

templates = create_templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(prefix="/assistant", include_in_schema=False)
_MIN_REQUEST_INTERVAL_SECONDS = 3.0


@router.get("", response_class=HTMLResponse)
def assistant_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    site_id: Annotated[list[int] | None, Query()] = None,
    site_scope: Literal["all", "selected"] = "selected",
):
    user = _current_user(request)
    provider = AiProviderConfigService(db=db, cipher=get_secret_cipher()).get_openai_config()
    selection = _assistant_selection(db, site_ids=site_id, site_scope=site_scope)
    return templates.TemplateResponse(
        request,
        "assistant.html",
        _page_context(
            request,
            provider_configured=provider is not None and provider.enabled,
            user=user,
            selection=selection,
        ),
    )


@router.post("", response_class=HTMLResponse)
def ask_assistant(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    question: Annotated[str, Form()] = "",
    site_id: Annotated[list[int] | None, Form()] = None,
    site_scope: Annotated[Literal["all", "selected"], Form()] = "selected",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _current_user(request)
    provider = AiProviderConfigService(db=db, cipher=get_secret_cipher()).get_openai_config()
    selection = _assistant_selection(db, site_ids=site_id, site_scope=site_scope)
    context = _page_context(
        request,
        provider_configured=provider is not None and provider.enabled,
        user=user,
        question=question,
        selection=selection,
    )
    if provider is None or not provider.enabled:
        context["error"] = "Connect OpenAI in Account before using the assistant."
        return templates.TemplateResponse(request, "assistant.html", context, status_code=400)

    try:
        _require_request_interval(request)
        answer = HubAssistantService(db=db, cipher=get_secret_cipher()).answer(
            question,
            previous_site_ids=_assistant_previous_site_ids(request),
            selected_site_ids=selection["selected_site_ids"],
            selection_is_explicit=selection["is_explicit"],
            selection_label=selection["label"],
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
    if answer.selection_site_ids is not None:
        selection = _assistant_selection(
            db,
            site_ids=list(answer.selection_site_ids),
            site_scope="selected",
        )
        context["site_selector"] = selection["site_selector"]
        context["assistant_selection"] = selection
        request.session["assistant_previous_site_ids"] = list(answer.selection_site_ids)

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
                f"for {answer.action.update_label} "
                f"in batch {outcome.batch_id[:12]}."
            )
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-assistant",
            action="assistant-direct-update-command",
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


def _page_context(request: Request, *, provider_configured: bool, user, selection: dict, question: str = "") -> dict:
    return {
        "csrf_token": get_csrf_token(request),
        "provider_configured": provider_configured,
        "question": question,
        "user": user,
        "site_selector": selection["site_selector"],
        "assistant_selection": selection,
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


def _assistant_selection(
    db: Session,
    *,
    site_ids: list[int] | None,
    site_scope: Literal["all", "selected"],
) -> dict:
    inventory = FleetInventoryService(db=db, cipher=get_secret_cipher())
    sites = sorted(
        (item.site for item in inventory.list_items(limit=1000)),
        key=lambda site: site.domain.casefold(),
    )
    available_site_ids = {site.id for site in sites}
    selected_ids = set(site_ids or []) & available_site_ids
    selected_site_ids = None if site_scope == "all" else selected_ids
    is_explicit = site_scope == "all" or bool(selected_ids)
    label = "allen Websites im Seitenpanel" if site_scope == "all" else "den im Seitenpanel ausgewaehlten Websites"
    return {
        "site_selector": build_site_selector_context(
            action="/assistant",
            form_id="assistant-site-scope-form",
            sites=sites,
            selected_site_ids=selected_site_ids,
            site_scope=site_scope,
            submit_label="Set assistant scope",
            target_form_id="assistant-question-form",
            hide_submit=True,
        ),
        "selected_site_ids": selected_site_ids,
        "is_explicit": is_explicit,
        "label": label,
    }
