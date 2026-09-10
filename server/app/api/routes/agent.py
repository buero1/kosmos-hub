"""The Hub-Agent capability page and persistent chat API."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.csrf import get_csrf_token, require_csrf
from app.core.security import get_secret_cipher
from app.core.templates import create_templates
from app.db.session import get_db
from app.models.hub_agent import HubAgentAction
from app.services.ai_provider import AiProviderConfigService
from app.services.audit import write_audit_log
from app.services.hub_agent import HubAgentChatView, HubAgentError, HubAgentService
from app.services.email_ai_prompt_presets import (
    EmailAiPromptPresetError,
    EmailAiPromptPresetInput,
    EmailAiPromptPresetService,
)

templates = create_templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(prefix="/agent", include_in_schema=False)


@router.get("", response_class=HTMLResponse)
def agent_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _current_user(request)
    cipher = get_secret_cipher()
    provider = AiProviderConfigService(db=db, cipher=cipher).get_openai_config()
    agent_service = HubAgentService(db=db, cipher=cipher)
    return templates.TemplateResponse(
        request,
        "agent.html",
        {
            "user": user,
            "provider_configured": provider is not None and provider.enabled,
            "capabilities": HubAgentService.capabilities(),
            "email_ai_prompt_presets": EmailAiPromptPresetService(db=db).list_presets(),
            "completed_chats": agent_service.list_archived_conversations(actor=user.username, status="completed"),
            "deleted_chats": agent_service.list_archived_conversations(actor=user.username, status="deleted"),
        },
    )


@router.post("/email-ai-prompts")
def save_email_ai_prompt_presets(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
    preset_id: Annotated[list[int], Form()] = [],
    label: Annotated[list[str], Form()] = [],
    instruction: Annotated[list[str], Form()] = [],
    enabled_preset_id: Annotated[list[int], Form()] = [],
    new_label: Annotated[str, Form()] = "",
    new_instruction: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _current_user(request)
    if len(preset_id) != len(label) or len(preset_id) != len(instruction):
        raise HTTPException(status_code=400, detail="Die E-Mail-Schnellaktionen sind unvollständig.")
    service = EmailAiPromptPresetService(db=db)
    enabled_preset_ids = set(enabled_preset_id)
    try:
        service.update_presets(
            actor=user,
            presets=tuple(
                EmailAiPromptPresetInput(
                    preset_id=identifier,
                    label=entry_label,
                    instruction=entry_instruction,
                    is_enabled=identifier in enabled_preset_ids,
                )
                for identifier, entry_label, entry_instruction in zip(preset_id, label, instruction, strict=True)
            ),
        )
        if new_label.strip() or new_instruction.strip():
            service.add_preset(actor=user, label=new_label, instruction=new_instruction)
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-agent",
            action="configure-email-ai-prompt-presets",
            result="success",
            detail="Updated the configurable email AI quick actions.",
        )
        db.commit()
    except EmailAiPromptPresetError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/agent?email_ai_prompts=saved#agent-email-ai-prompts", status_code=303)


@router.get("/chat")
def agent_chat(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    conversation_id: Annotated[int | None, Query()] = None,
) -> JSONResponse:
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        chat = service.chat_view(actor=user.username, conversation_id=conversation_id)
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        return _chat_error(str(exc), status_code=400)
    return JSONResponse(_chat_payload(request, chat))


@router.post("/chat/new")
def start_agent_chat(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
) -> JSONResponse:
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    chat = service.start_conversation(actor=user.username)
    db.commit()
    return JSONResponse(_chat_payload(request, chat))


@router.post("/chat/context")
def add_agent_context(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    resource_type: Annotated[str, Form()] = "",
    resource_key: Annotated[str, Form()] = "",
    conversation_id: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> JSONResponse:
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        chat = service.add_context(
            actor=user.username,
            resource_type=resource_type,
            resource_key=resource_key,
            conversation_id=_optional_id(conversation_id),
        )
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-agent",
            action="agent-context-added",
            result="success",
            detail=f"Added {resource_type.strip()[:48]} context to Hub agent conversation {chat.conversation_id}.",
        )
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        return _chat_error(str(exc), status_code=400)
    return JSONResponse(_chat_payload(request, chat))


@router.post("/chat/context/{context_id}/remove")
def remove_agent_context(
    context_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    conversation_id: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> JSONResponse:
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        chat = service.remove_context(
            actor=user.username,
            conversation_id=_required_id(conversation_id),
            context_id=context_id,
        )
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        return _chat_error(str(exc), status_code=400)
    return JSONResponse(_chat_payload(request, chat))


@router.post("/chat/{conversation_id}/close")
def close_agent_chat(
    conversation_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
) -> JSONResponse:
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        chat = service.close_conversation(actor=user.username, conversation_id=conversation_id)
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        return _chat_error(str(exc), status_code=400)
    return JSONResponse(_chat_payload(request, chat))


@router.post("/chat/{conversation_id}/delete")
def delete_agent_chat(
    conversation_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
) -> JSONResponse:
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        chat = service.delete_conversation(actor=user.username, conversation_id=conversation_id)
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-agent",
            action="agent-conversation-deleted",
            result="success",
            detail=f"Hub agent conversation {conversation_id} was moved to deleted chats.",
        )
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        return _chat_error(str(exc), status_code=400)
    return JSONResponse(_chat_payload(request, chat))


@router.post("/chat/messages")
def send_agent_message(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    conversation_id: Annotated[str, Form()] = "",
    instruction: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> JSONResponse:
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        chat = service.chat(
            actor=user.username,
            conversation_id=_required_id(conversation_id),
            instruction=instruction,
        )
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-agent",
            action="agent-message-planned",
            result="success",
            detail=f"Created a controlled Hub agent plan in conversation {chat.conversation_id}. Request content is encrypted and omitted from the audit log.",
        )
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        return _chat_error(str(exc), status_code=400)
    return JSONResponse(_chat_payload(request, chat))


@router.post("/chat/actions/{action_id}/execute")
def execute_agent_chat_action(
    action_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
) -> JSONResponse:
    require_csrf(request, csrf_token)
    user = _current_user(request)
    service = HubAgentService(db=db, cipher=get_secret_cipher())
    try:
        action = service.execute_action(action_id=action_id, actor=user.username)
        stored_action = db.get(HubAgentAction, action_id)
        if stored_action is None or stored_action.job.conversation_id is None:
            raise HubAgentError("Diese Aktion gehört zu keiner Agent-Unterhaltung.")
        chat = service.chat_view(actor=user.username, conversation_id=stored_action.job.conversation_id)
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="hub-agent",
            action=f"agent-{action.action_type}",
            result="success" if action.status == "completed" else "error",
            detail=f"Hub agent action {action.id} finished with status {action.status}. Sensitive action data is encrypted and omitted from the audit log.",
        )
        db.commit()
    except HubAgentError as exc:
        db.rollback()
        return _chat_error(str(exc), status_code=400)
    return JSONResponse(_chat_payload(request, chat))


def _chat_payload(request: Request, chat: HubAgentChatView) -> dict[str, object]:
    return {
        "csrf_token": get_csrf_token(request),
        "conversation": {
            "id": chat.conversation_id,
            "title": chat.title,
            "status": chat.status,
        },
        "contexts": [
            {
                "id": context.id,
                "type": context.resource_type,
                "key": context.resource_key,
                "label": context.label,
                "description": context.description,
            }
            for context in chat.contexts
        ],
        "messages": [
            {
                "id": job.id,
                "instruction": job.request_text,
                "summary": job.summary,
                "response": job.response_text,
                "status": job.status,
                "created_at": job.created_at.isoformat(),
                "actions": [
                    {
                        "id": action.id,
                        "type": action.action_type,
                        "title": action.title,
                        "details": action.details,
                        "preview_lines": list(action.preview_lines),
                        "status": action.status,
                        "result_label": action.result_label,
                        "result_href": action.result_href,
                        "error": action.error,
                    }
                    for action in job.actions
                ],
            }
            for job in chat.jobs
        ],
        "conversations": [
            {
                "id": conversation.id,
                "title": conversation.title,
                "status": conversation.status,
                "updated_at": conversation.updated_at.isoformat(),
                "context_count": conversation.context_count,
            }
            for conversation in chat.conversations
        ],
    }


def _chat_error(message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _optional_id(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    return _required_id(text)


def _required_id(value: str) -> int:
    try:
        identifier = int(value.strip())
    except ValueError as exc:
        raise HubAgentError("Die Unterhaltung ist ungültig.") from exc
    if identifier <= 0:
        raise HubAgentError("Die Unterhaltung ist ungültig.")
    return identifier


def _current_user(request: Request):
    user = getattr(request.state, "hub_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user
