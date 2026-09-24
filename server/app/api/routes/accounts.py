import json
from datetime import date
from pathlib import Path
from secrets import compare_digest
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.csrf import get_csrf_token, require_csrf
from app.core.security import get_secret_cipher
from app.core.templates import create_templates
from app.db.session import get_db
from app.services.audit import write_audit_log
from app.services.hub_operations import HubOperationService, HubOperationError
from app.services.hub_administration import HubAdministrationService, runtime_settings
from app.services.hub_activity import MODULE_LABELS, list_activity_events
from app.services.ai_provider import AiProviderConfigError, AiProviderConfigService
from app.services.ai_models import MODEL_PROFILES
from app.services.crocoblock_license import CrocoblockLicenseError, CrocoblockLicenseService
from app.services.fleet_refresh_settings import FleetRefreshSettingsError, FleetRefreshSettingsService
from app.services.hub_accounts import HUB_USER_ROLES, HubAccountService
from app.services.hub_access_control import (
    ACCESS_ACTIONS,
    ACCESS_ACTION_LABELS,
    ACCESS_MODULES,
    ACCESS_SCOPE_LABELS,
    HubAccessControlService,
    can_open_settings,
)
from app.services.hub_mailbox_accounts import HubMailboxAccountError, HubMailboxAccountService
from app.services.hub_mailbox_imap_sync import HubMailboxImapSyncService
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.services.hub_spam_senders import HubSpamSenderService
from app.services.hub_workflows import HubWorkflowService
from app.services.hub_legal_terms import HubLegalTermsError, HubLegalTermsService
from app.services.hub_pdf_templates import (
    PDF_LINE_SOURCES,
    PDF_TEMPLATE_TYPES,
    HubPdfTemplateError,
    HubPdfTemplateService,
)
from app.services.hub_mailbox_imap_import import HubMailboxImapImportError, HubMailboxImapImportService
from app.services.provider_credentials import ProviderCredentialError, ProviderCredentialService
from app.services.zoho_crm import ZOHO_DATA_CENTERS, ZohoCrmError, ZohoCrmService
from app.services.zoho_books import ZohoBooksError, ZohoBooksService
from app.services.zoho_books_invoice_import import ZohoBooksInvoiceImportService
from app.services.zoho_books_order_import import ZohoBooksOrderImportService
from app.services.zoho_books_recurring_invoice_import import ZohoBooksRecurringInvoiceImportService
from app.services.finance_invoice_pdf_storage import FinanceInvoicePdfStorageError
from app.services.customer_communications import CustomerCommunicationService
from app.services.email_composer_settings import (
    FONT_FAMILY_OPTIONS,
    FONT_SIZE_OPTIONS,
    LINE_HEIGHT_OPTIONS,
    EmailComposerSettingsError,
    EmailComposerSettingsService,
)
from app.services.styling_settings import (
    FONT_FAMILY_OPTIONS as STYLING_FONT_FAMILY_OPTIONS,
    StylingSettingsService,
)
from app.services.email_attachment_storage import EmailAttachmentStorageError
from app.services.email_compose_images import EmailComposeImageService
from app.services.template_placeholders import USER_PLACEHOLDERS
from app.services.maintenance_worker import (
    schedule_pending_zoho_email_attachment_import,
    schedule_pending_zoho_email_content_import,
    schedule_pending_zoho_email_history_import,
    schedule_pending_zoho_note_history_import,
    schedule_pending_zoho_books_invoice_import,
    schedule_pending_zoho_books_order_import,
    schedule_pending_zoho_books_recurring_invoice_import,
    schedule_pending_zoho_email_workflow_deliveries,
    schedule_pending_hub_mailbox_imap_import,
)
from app.services.zoho_email_history_import import ZohoEmailHistoryImportService
from app.services.zoho_email_content_import import ZohoEmailContentImportService
from app.services.zoho_email_attachment_import import ZohoEmailAttachmentImportService
from app.services.zoho_note_history_import import ZohoNoteHistoryImportService
from app.services.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhookService

templates = create_templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
router = APIRouter(prefix="/account", include_in_schema=False)
bootstrap_router = APIRouter(include_in_schema=False)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = ""):
    if _current_user(request) is not None:
        return RedirectResponse(url=_safe_next(next) or "/", status_code=303)
    return templates.TemplateResponse(request, "account_login.html", {"next": _safe_next(next), "csrf_token": get_csrf_token(request)})


@router.post("/login")
def login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    service = _account_service(db)
    try:
        user = service.authenticate(username, password)
    except ValueError:
        user = None
    if user is None:
        return templates.TemplateResponse(
            request,
            "account_login.html",
            {"next": _safe_next(next), "csrf_token": get_csrf_token(request), "error": "Benutzername oder Passwort ist falsch."},
            status_code=400,
        )
    request.session.clear()
    request.session.update({"user_id": user.id, "session_version": user.session_version})
    write_audit_log(db, site=None, actor=user.username, source="hub-account", action="login", result="success")
    db.commit()
    return RedirectResponse(url=_safe_next(next) or "/", status_code=303)


@router.post("/logout")
def logout(request: Request, db: Annotated[Session, Depends(get_db)], csrf_token: Annotated[str, Form()] = ""):
    require_csrf(request, csrf_token)
    user = _require_current_user(request)
    write_audit_log(db, site=None, actor=user.username, source="hub-account", action="logout", result="success")
    db.commit()
    request.session.clear()
    return RedirectResponse(url="/account/login", status_code=303)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    return templates.TemplateResponse(request, "account_setup.html", {"csrf_token": get_csrf_token(request)})


@router.post("/setup")
def setup_first_admin(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    setup_token: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirmation: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    service = _account_service(db)
    try:
        user = service.create_first_admin(
            token=setup_token,
            username=username,
            password=password,
            password_confirmation=password_confirmation,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account_setup.html",
            {"csrf_token": get_csrf_token(request), "error": str(exc)},
            status_code=400,
        )
    write_audit_log(db, site=None, actor=user.username, source="hub-account", action="create-first-admin", result="success")
    db.commit()
    return RedirectResponse(url="/account/login?setup=complete", status_code=303)


@router.get("", response_class=HTMLResponse)
def account_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    service = _account_service(db)
    user = _require_persisted_current_user(request, service)
    settings_query_sections = (
        ("fleet_refresh", "account-refresh-settings"),
        ("legal_terms", "account-legal-terms"),
        ("legal_terms_state", "account-legal-terms"),
        ("pdf_template", "account-pdf-templates"),
        ("pdf_template_type", "account-pdf-templates"),
        ("pdf_template_state", "account-pdf-templates"),
        ("mailbox", "account-mailbox"),
        ("spam_sender", "account-mailbox"),
        ("email_composer", "account-mailbox"),
        ("email_signature", "account-mailbox"),
        ("zoho_books", "account-zoho-books"),
        ("zoho", "account-zoho"),
        ("styling", "account-styling"),
    )
    for query_key, section_id in settings_query_sections:
        if query_key in request.query_params:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(url=f"/settings{query}#{section_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "account.html",
        _account_context(request, user, service, page_mode="account"),
    )


@bootstrap_router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    service = _account_service(db)
    user = _require_persisted_current_user(request, service)
    if not can_open_settings(user, can_view=HubAccessControlService(db=db).can(user, "settings", "view")):
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return templates.TemplateResponse(
        request,
        "account.html",
        _account_context(request, user, service, page_mode="settings"),
    )


def _access_redirect(state: str, message: str = "") -> RedirectResponse:
    query = urlencode({"access": state, "access_message": message})
    return RedirectResponse(url=f"/settings?{query}#account-access", status_code=303)


def _access_permission_values(form: object) -> dict[str, dict[str, object]]:
    return {
        module.key: {
            **{
                action: str(form.get(f"permission__{module.key}__{action}") or "") == "1"
                for action in ACCESS_ACTIONS
            },
            "scope": str(form.get(f"permission__{module.key}__scope") or ("all" if not module.scope_configurable else "none")),
        }
        for module in ACCESS_MODULES
    }


@router.post("/access/roles")
async def create_access_role(request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    actor = _require_admin_user(request)
    try:
        role = _administration(db, actor).save_role(
            role_key=str(form.get("role_key") or ""),
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            permissions=_access_permission_values(form),
        )
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="create-role", result="success", detail=f"Created access role {role.key}.")
    db.commit()
    return _access_redirect("role-saved")


@router.post("/access/roles/{role_key}")
async def update_access_role(role_key: str, request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    actor = _require_admin_user(request)
    try:
        role = _administration(db, actor).save_role(
            role_key=role_key,
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            permissions=_access_permission_values(form),
        )
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="update-role", result="success", detail=f"Updated access role {role.key}.")
    db.commit()
    return _access_redirect("role-saved")


@router.post("/access/roles/{role_key}/delete")
def delete_access_role(
    role_key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    try:
        _administration(db, actor).delete_role(role_key)
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="delete-role", result="success", detail=f"Deleted access role {role_key}.")
    db.commit()
    return _access_redirect("role-deleted")


@router.post("/access/teams")
def create_access_team(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    try:
        team = _administration(db, actor).create_team(name=name, description=description)
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="create-team", result="success", detail=f"Created team {team.id}.")
    db.commit()
    return _access_redirect("team-saved")


@router.post("/access/teams/{team_id}")
def update_access_team(
    team_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    try:
        team = _administration(db, actor).update_team(team_id=team_id, name=name, description=description)
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="update-team", result="success", detail=f"Updated team {team.id}.")
    db.commit()
    return _access_redirect("team-saved")


@router.post("/access/teams/{team_id}/delete")
def delete_access_team(
    team_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    try:
        _administration(db, actor).delete_team(team_id)
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="delete-team", result="success", detail=f"Deleted team {team_id}.")
    db.commit()
    return _access_redirect("team-deleted")


@router.post("/access/users/{user_id}")
def update_user_access(
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    role: Annotated[str, Form()] = "viewer",
    team_id: Annotated[str, Form()] = "",
    email_address: Annotated[str | None, Form()] = None,
    default_sender_account_id: Annotated[str | None, Form()] = None,
    first_name: Annotated[str | None, Form()] = None,
    last_name: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    service = _account_service(db)
    target = service.get_user(user_id)
    if target is None:
        return _access_redirect("error", "Dieser Hub-Benutzer wurde nicht gefunden.")
    try:
        updated = _administration(db, actor).update_user(
            user_id=target.id,
            username=target.username,
            role=role,
            team_id=int(team_id) if team_id.isdigit() else None,
            reminder_email=target.reminder_email or "",
            email_address=email_address,
            default_sender_account_id=default_sender_account_id,
            first_name=first_name,
            last_name=last_name,
        )
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="update-user-access", result="success", detail=f"Updated role and team for Hub user {updated.id}.")
    db.commit()
    if updated.id == actor.id:
        request.session.clear()
        request.session.update({"user_id": updated.id, "session_version": updated.session_version})
    return _access_redirect("user-saved")


@router.post("/access/mailboxes/{subject_type}/{subject_id}")
async def save_mailbox_access(request: Request, subject_type: str, subject_id: int, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token", "")))
    actor = _require_admin_user(request)
    from app.services.hub_mailbox_permissions import MAILBOX_ACTIONS
    entries = {}
    try:
        for account_id in form.getlist("mailbox_id"):
            values = {}
            for action in MAILBOX_ACTIONS:
                raw = str(form.get(f"mailbox__{account_id}__{action}", "false" if subject_type == "team" else "inherit"))
                if raw not in {"true", "false", "inherit"}:
                    raise ValueError("Ungültige Postfachberechtigung.")
                values[action] = {"true": True, "false": False, "inherit": None}[raw]
            entries[str(account_id)] = values
        HubOperationService(db=db, cipher=get_secret_cipher(), actor=actor.username).execute("access.mailboxes.update", {
            "subject_type": subject_type, "subject_id": str(subject_id), "permissions": json.dumps(entries),
        })
        write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="update-mailbox-access", result="success", detail=f"Updated mailbox grants for {subject_type} {subject_id}.")
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    return _access_redirect("mailbox-saved")


@router.get("/access/records/options")
def access_record_options(request: Request, db: Annotated[Session, Depends(get_db)],
                          module_key: str = "customers", query: str = "", offset: str = "0", limit: str = "100", team_id: str = ""):
    actor = _require_admin_user(request)
    try:
        data = HubOperationService(db=db, cipher=get_secret_cipher(), actor=actor.username).query(
            "access.records.options", {"module_key": module_key, "query": query, "offset": offset, "limit": limit, "team_id": team_id})
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400, headers={"Cache-Control": "no-store"})
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.post("/access/records/batch")
async def save_record_access_batch(request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    actor = _require_admin_user(request)
    try:
        operation = {"assign": "access.records.assign_many", "grant": "access.grants.create_many"}.get(form.get("kind"))
        if operation is None:
            raise ValueError("Ungueltige Zuweisungsart.")
        values = {key: value for key, value in form.items() if key not in {"csrf_token", "kind"}
                  and not (key in {"owner_user_id", "team_id"} and value == "__keep__")}
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=actor.username).execute(operation, values)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"detail": str(exc)}, status_code=400, headers={"Cache-Control": "no-store"})
    state = "assignment-saved" if form.get("kind") == "assign" else "grant-saved"
    return JSONResponse({"message": result.label, "changed": result.outputs["changed"],
                         "redirect_url": f"/settings?access={state}#account-access"}, headers={"Cache-Control": "no-store"})


@router.post("/access/assignments")
def save_record_assignment(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    module_key: Annotated[str, Form()] = "",
    record_id: Annotated[str, Form()] = "",
    owner_user_id: Annotated[str, Form()] = "",
    team_id: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    try:
        if not record_id.isdigit():
            raise ValueError("Bitte eine gültige Datensatz-ID eingeben.")
        assignment = _administration(db, actor).assign_record(
            module_key=module_key,
            record_id=int(record_id),
            owner_user_id=int(owner_user_id) if owner_user_id.isdigit() else None,
            team_id=int(team_id) if team_id.isdigit() else None,
        )
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="assign-record", result="success", detail=f"Assigned {assignment.module_key} record {assignment.record_id}.")
    db.commit()
    return _access_redirect("assignment-saved")


@router.post("/access/grants")
def create_record_grant(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    module_key: Annotated[str, Form()] = "",
    record_id: Annotated[str, Form()] = "",
    user_id: Annotated[str, Form()] = "",
    team_id: Annotated[str, Form()] = "",
    can_edit: Annotated[bool, Form()] = False,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    try:
        if not record_id.isdigit():
            raise ValueError("Bitte eine gültige Datensatz-ID eingeben.")
        grant = _administration(db, actor).add_grant(
            module_key=module_key,
            record_id=int(record_id),
            user_id=int(user_id) if user_id.isdigit() else None,
            team_id=int(team_id) if team_id.isdigit() else None,
            can_edit=can_edit,
        )
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="grant-record", result="success", detail=f"Granted {grant.module_key} record {grant.record_id}.")
    db.commit()
    return _access_redirect("grant-saved")


@router.post("/access/grants/{grant_id}/delete")
def delete_record_grant(
    grant_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    try:
        _administration(db, actor).delete_grant(grant_id)
    except ValueError as exc:
        db.rollback()
        return _access_redirect("error", str(exc))
    write_audit_log(db, site=None, actor=actor.username, source="hub-access", action="revoke-record", result="success", detail=f"Revoked record grant {grant_id}.")
    db.commit()
    return _access_redirect("grant-deleted")


@router.post("/legal-terms")
def create_legal_terms(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubLegalTermsService(db=db)
    try:
        legal_terms = _execute_document_template_operation(db, user, "legal_terms", "create", name=name)
    except HubLegalTermsError as exc:
        db.rollback()
        return _legal_terms_error_response(request, user, db, str(exc))
    _audit_legal_terms(db, actor=user.username, action="create", legal_terms=legal_terms)
    db.commit()
    return _legal_terms_redirect(legal_terms.id, "created")


@router.post("/legal-terms/{legal_terms_id}")
def update_legal_terms(
    legal_terms_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    content_html: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubLegalTermsService(db=db)
    try:
        legal_terms = _execute_document_template_operation(
            db, user, "legal_terms", "update",
            legal_terms_id=legal_terms_id,
            name=name,
            content_html=content_html,
        )
    except HubLegalTermsError as exc:
        db.rollback()
        return _legal_terms_error_response(
            request,
            user,
            db,
            str(exc),
            selected_legal_terms_id=legal_terms_id,
        )
    _audit_legal_terms(db, actor=user.username, action="update", legal_terms=legal_terms)
    db.commit()
    return _legal_terms_redirect(legal_terms.id, "saved")


@router.post("/legal-terms/{legal_terms_id}/duplicate")
def duplicate_legal_terms(
    legal_terms_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubLegalTermsService(db=db)
    try:
        legal_terms = _execute_document_template_operation(db, user, "legal_terms", "duplicate", legal_terms_id=legal_terms_id)
    except HubLegalTermsError as exc:
        db.rollback()
        return _legal_terms_error_response(
            request,
            user,
            db,
            str(exc),
            selected_legal_terms_id=legal_terms_id,
        )
    _audit_legal_terms(db, actor=user.username, action="duplicate", legal_terms=legal_terms)
    db.commit()
    return _legal_terms_redirect(legal_terms.id, "duplicated")


@router.post("/legal-terms/{legal_terms_id}/delete")
def delete_legal_terms(
    legal_terms_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubLegalTermsService(db=db)
    legal_terms = service.get(legal_terms_id)
    try:
        _execute_document_template_operation(db, user, "legal_terms", "delete", legal_terms_id=legal_terms_id)
    except HubLegalTermsError as exc:
        db.rollback()
        return _legal_terms_error_response(
            request,
            user,
            db,
            str(exc),
            selected_legal_terms_id=legal_terms_id,
        )
    if legal_terms is not None:
        _audit_legal_terms(db, actor=user.username, action="delete", legal_terms=legal_terms)
    db.commit()
    return _legal_terms_redirect(None, "deleted")


@router.post("/pdf-templates")
def create_pdf_template(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    document_type: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubPdfTemplateService(db=db)
    try:
        template = _execute_document_template_operation(db, user, "pdf_templates", "create", document_type=document_type, name=name)
    except HubPdfTemplateError as exc:
        db.rollback()
        return _pdf_template_error_response(request, user, db, str(exc), selected_type=document_type)
    _audit_pdf_template(db, actor=user.username, action="create", template=template)
    db.commit()
    return _pdf_template_redirect(template.id, template.document_type, "created")


@router.post("/pdf-templates/{template_id}/rename")
def rename_pdf_template(
    template_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubPdfTemplateService(db=db)
    try:
        template = _execute_document_template_operation(db, user, "pdf_templates", "rename", template_id=template_id, name=name)
    except HubPdfTemplateError as exc:
        db.rollback()
        return _pdf_template_error_response(request, user, db, str(exc), selected_template_id=template_id)
    _audit_pdf_template(db, actor=user.username, action="rename", template=template)
    db.commit()
    return _pdf_template_redirect(template.id, template.document_type, "saved")


@router.post("/pdf-templates/{template_id}/blocks/{block_key}")
def update_pdf_template_block(
    template_id: int,
    block_key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    content_html: Annotated[str, Form()] = "",
    is_visible: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubPdfTemplateService(db=db)
    try:
        template = _execute_document_template_operation(
            db, user, "pdf_templates", "update_block",
            template_id=template_id,
            block_key=block_key,
            content_html=content_html,
            is_visible=is_visible == "true",
        )
    except HubPdfTemplateError as exc:
        db.rollback()
        return _pdf_template_error_response(request, user, db, str(exc), selected_template_id=template_id)
    _audit_pdf_template(db, actor=user.username, action=f"update-block:{block_key}", template=template)
    db.commit()
    return _pdf_template_redirect(template.id, template.document_type, "saved")


@router.post("/pdf-templates/{template_id}/legal-terms")
def update_pdf_template_legal_terms(
    template_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    legal_terms_id: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    selected_legal_terms_id = None
    if legal_terms_id.strip():
        if not legal_terms_id.isdigit() or int(legal_terms_id) <= 0:
            return _pdf_template_error_response(
                request,
                user,
                db,
                "Die ausgewählte AGB ist ungültig.",
                selected_template_id=template_id,
            )
        selected_legal_terms_id = int(legal_terms_id)
    service = HubPdfTemplateService(db=db)
    try:
        template = _execute_document_template_operation(
            db, user, "pdf_templates", "set_legal_terms",
            template_id=template_id,
            legal_terms_id=selected_legal_terms_id,
        )
    except HubPdfTemplateError as exc:
        db.rollback()
        return _pdf_template_error_response(request, user, db, str(exc), selected_template_id=template_id)
    _audit_pdf_template(db, actor=user.username, action="set-legal-terms", template=template)
    db.commit()
    return _pdf_template_redirect(template.id, template.document_type, "saved")


@router.post("/pdf-templates/{template_id}/positions")
async def update_pdf_template_positions(
    template_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token", "")))
    user = _require_admin_user(request)
    keys = [str(value) for value in form.getlist("column_key")]
    labels = [str(value) for value in form.getlist("column_label")]
    sources = [str(value) for value in form.getlist("column_source")]
    widths = [str(value) for value in form.getlist("column_width")]
    alignments = [str(value) for value in form.getlist("column_alignment")]
    enabled = {str(value) for value in form.getlist("column_enabled")}
    columns = [
        {
            "key": key,
            "label": label,
            "source_key": source,
            "width": width,
            "alignment": alignment,
            "is_enabled": key in enabled,
        }
        for key, label, source, width, alignment in zip(
            keys,
            labels,
            sources,
            widths,
            alignments,
            strict=False,
        )
    ]
    service = HubPdfTemplateService(db=db)
    try:
        template = _execute_document_template_operation(db, user, "pdf_templates", "update_positions",
            template_id=template_id, columns=columns, show_totals=form.get("show_totals") == "true")
    except HubPdfTemplateError as exc:
        db.rollback()
        return _pdf_template_error_response(request, user, db, str(exc), selected_template_id=template_id)
    _audit_pdf_template(db, actor=user.username, action="update-positions", template=template)
    db.commit()
    return _pdf_template_redirect(template.id, template.document_type, "saved")


@router.post("/pdf-templates/{template_id}/duplicate")
def duplicate_pdf_template(
    template_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubPdfTemplateService(db=db)
    try:
        template = _execute_document_template_operation(db, user, "pdf_templates", "duplicate", template_id=template_id)
    except HubPdfTemplateError as exc:
        db.rollback()
        return _pdf_template_error_response(request, user, db, str(exc), selected_template_id=template_id)
    _audit_pdf_template(db, actor=user.username, action="duplicate", template=template)
    db.commit()
    return _pdf_template_redirect(template.id, template.document_type, "duplicated")


@router.post("/pdf-templates/{template_id}/default")
def set_default_pdf_template(
    template_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubPdfTemplateService(db=db)
    try:
        template = _execute_document_template_operation(db, user, "pdf_templates", "set_default", template_id=template_id)
    except HubPdfTemplateError as exc:
        db.rollback()
        return _pdf_template_error_response(request, user, db, str(exc), selected_template_id=template_id)
    _audit_pdf_template(db, actor=user.username, action="set-default", template=template)
    db.commit()
    return _pdf_template_redirect(template.id, template.document_type, "default-set")


@router.post("/pdf-templates/{template_id}/delete")
def delete_pdf_template(
    template_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = HubPdfTemplateService(db=db)
    template = service.get(template_id)
    try:
        document_type = _execute_document_template_operation(db, user, "pdf_templates", "delete", template_id=template_id)
    except HubPdfTemplateError as exc:
        db.rollback()
        return _pdf_template_error_response(request, user, db, str(exc), selected_template_id=template_id)
    if template is not None:
        _audit_pdf_template(db, actor=user.username, action="delete", template=template)
    db.commit()
    return _pdf_template_redirect(None, document_type, "deleted")


@router.post("/password")
def change_password(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    password_confirmation: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    current_user = _require_current_user(request)
    service = _account_service(db)
    user = service.get_user(current_user.id)
    if user is None:
        request.session.clear()
        return RedirectResponse(url="/account/login", status_code=303)
    try:
        service.change_password(
            user=user,
            current_password=current_password,
            new_password=new_password,
            password_confirmation=password_confirmation,
        )
    except ValueError as exc:
        error_section = "account-users" if user.role == "admin" else "account-security"
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc), error_section=error_section),
            status_code=400,
        )
    write_audit_log(db, site=None, actor=user.username, source="hub-account", action="change-password", result="success")
    db.commit()
    request.session.clear()
    request.session.update({"user_id": user.id, "session_version": user.session_version})
    destination = "account-users" if user.role == "admin" else "account-security"
    return RedirectResponse(url=f"/account?password=changed#{destination}", status_code=303)


@router.post("/users")
def create_hub_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirmation: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "viewer",
    team_id: Annotated[str, Form()] = "",
    email_address: Annotated[str, Form()] = "",
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    service = _account_service(db)
    try:
        user = _administration(db, actor).create_user(
            username=username,
            password=password,
            password_confirmation=password_confirmation,
            role=role,
            team_id=int(team_id) if team_id.isdigit() else None,
            email_address=email_address,
            first_name=first_name,
            last_name=last_name,
        )
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, actor, service, error=str(exc), error_section="account-users"),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=actor.username,
        source="hub-account",
        action="create-hub-user",
        result="success",
        detail=f"Created Hub user {user.username} with role {user.role}.",
    )
    db.commit()
    return RedirectResponse(url=f"/account?{urlencode({'user': 'created', 'user_id': user.id})}#account-users", status_code=303)


@router.post("/users/{user_id}")
def update_hub_user(
    request: Request,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "viewer",
    team_id: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirmation: Annotated[str, Form()] = "",
    reminder_email: Annotated[str, Form()] = "",
    email_address: Annotated[str | None, Form()] = None,
    default_sender_account_id: Annotated[str | None, Form()] = None,
    first_name: Annotated[str | None, Form()] = None,
    last_name: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    service = _account_service(db)
    try:
        user = _administration(db, actor).update_user(
            user_id=user_id,
            username=username,
            role=role,
            team_id=int(team_id) if team_id.isdigit() else None,
            password=password,
            password_confirmation=password_confirmation,
            reminder_email=reminder_email,
            email_address=email_address,
            default_sender_account_id=default_sender_account_id,
            first_name=first_name,
            last_name=last_name,
        )
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, actor, service, error=str(exc), error_section="account-users"),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=actor.username,
        source="hub-account",
        action="update-hub-user",
        result="success",
        detail=f"Updated Hub user {user.username} with role {user.role}.",
    )
    db.commit()
    if user.id == actor.id:
        request.session.clear()
        request.session.update({"user_id": user.id, "session_version": user.session_version})
    return RedirectResponse(url=f"/account?{urlencode({'user': 'updated', 'user_id': user.id})}#account-users", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_hub_user(
    request: Request,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    actor = _require_admin_user(request)
    service = _account_service(db)
    try:
        deleted_username, image_storage_keys = _administration(db, actor).delete_user(user_id=user_id)
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, actor, service, error=str(exc), error_section="account-users"),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=actor.username,
        source="hub-account",
        action="delete-hub-user",
        result="success",
        detail=f"Deleted Hub user {deleted_username}.",
    )
    db.commit()
    image_service = EmailComposeImageService(db=db, cipher=get_secret_cipher())
    for storage_key in image_storage_keys:
        image_service.storage.remove(storage_key)
    if user_id == actor.id:
        request.session.clear()
        return RedirectResponse(url="/account/login", status_code=303)
    return RedirectResponse(url="/account?user=deleted#account-users", status_code=303)


@router.post("/task-reminder-email")
def configure_task_reminder_email(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    reminder_email: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    service = _account_service(db)
    user = _require_persisted_current_user(request, service)
    try:
        _administration(db, user).configure_reminder_email(reminder_email=reminder_email)
    except ValueError as exc:
        error_section = "account-users" if user.role == "admin" else "account-security"
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc), error_section=error_section),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="configure-task-reminder-email",
        result="success",
        detail="Updated the personal recipient address for task email reminders.",
    )
    db.commit()
    destination = "account-users" if user.role == "admin" else "account-security"
    return RedirectResponse(url=f"/account?task_reminder_email=saved#{destination}", status_code=303)


@router.post("/mailboxes/mittwald")
def configure_mittwald_mailbox(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email_address: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    """Test and securely save one Mittwald mailbox without starting email synchronization."""
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    mailbox_service = HubMailboxAccountService(db=db, cipher=get_secret_cipher())
    try:
        account = mailbox_service.test_and_save(
            email_address=email_address,
            display_name=display_name,
            username=username,
            password=password,
            configured_by=user,
        )
    except HubMailboxAccountError as exc:
        db.commit()
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-mailbox",
        action="test-and-configure-mittwald-mailbox",
        result="success",
        detail=f"Verified Mittwald IMAP and SMTP access for mailbox {account.email_address}.",
    )
    db.commit()
    return RedirectResponse(url="/settings?mailbox=connected#account-mailbox", status_code=303)


@router.post("/mailboxes/alerts")
def configure_mailbox_alert_email(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    mailbox_alert_email: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    service = _account_service(db)
    user = _require_persisted_current_user(request, service)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    try:
        _administration(db, user).configure_mailbox_alert(mailbox_alert_email=mailbox_alert_email)
    except (HubMailboxAccountError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc), error_section="account-mailbox"),
            status_code=400,
        )
    write_audit_log(
        db, site=None, actor=user.username, source="hub-mailbox",
        action="configure-mailbox-alert-email", result="success",
        detail="Updated independent mailbox health alert recipient.",
    )
    db.commit()
    return RedirectResponse(url="/settings?mailbox=alert-saved#account-mailbox", status_code=303)


@router.post("/mailboxes/failed/{failure_id}/retry")
def retry_failed_mailbox_message(
    failure_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    sync = HubMailboxImapSyncService(
        db=db, cipher=get_secret_cipher(), public_base_url=get_settings().public_base_url,
    )
    try:
        succeeded = sync.retry_failed_message(failure_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    write_audit_log(
        db, site=None, actor=user.username, source="hub-mailbox",
        action="retry-failed-imap-message", result="success" if succeeded else "error",
        detail=f"Retried failed IMAP message {failure_id}.",
    )
    db.commit()
    result = "retry-success" if succeeded else "retry-failed"
    return RedirectResponse(url=f"/settings?mailbox={result}#account-mailbox", status_code=303)


@router.post("/mailboxes/spam-senders/{sender_id}/unblock")
def unblock_spam_sender(
    sender_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            "emails.spam_senders.unblock", {"sender_id": str(sender_id)})
    except HubOperationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-mailbox",
        action="unblock-spam-sender",
        result="success",
        detail=f"Removed spam sender rule {sender_id}; address is not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(url="/settings?spam_sender=unblocked#account-mailbox", status_code=303)


@router.post("/mailboxes/import")
def import_mittwald_mailboxes(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    since_date: Annotated[str, Form()] = "2026-09-05",
    csrf_token: Annotated[str, Form()] = "",
):
    """Select only the requested date range, then import it outside the web request."""
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        selected_date = date.fromisoformat(since_date)
        if selected_date > date.today():
            raise HubMailboxImapImportError("Das Startdatum darf nicht in der Zukunft liegen.")
        status, started = HubMailboxImapImportService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).start(requested_by=user.username, since_date=selected_date)
    except (HubMailboxImapImportError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-mailbox",
        action="start-mittwald-imap-import",
        result="success",
        detail=f"Selected {status.total_messages} Mittwald message(s) since {selected_date.isoformat()}.",
    )
    db.commit()
    schedule_pending_hub_mailbox_imap_import()
    state = "import-started" if started else "import-running"
    return RedirectResponse(url=f"/settings?mailbox={state}#account-mailbox", status_code=303)


@router.post("/mail-composer-settings")
def configure_mail_composer_settings(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    font_family_key: Annotated[str, Form()] = "verdana",
    font_size: Annotated[int, Form()] = 12,
    line_height: Annotated[float, Form()] = 1.1,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        configured = _administration(db, user).configure("email_composer",
            font_family_key=font_family_key,
            font_size=font_size,
            line_height=line_height,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="configure-email-composer-defaults",
        result="success",
        detail=(
            f"Set email composer defaults to {configured.font_family_key} at {configured.font_size}px, "
            f"line height {configured.line_height:g}."
        ),
    )
    db.commit()
    return RedirectResponse(url="/settings?email_composer=saved#account-mailbox", status_code=303)


@router.post("/mail-signature")
def configure_mail_signature(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    signature_html: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        _administration(db, user).configure_signature(signature_html=signature_html)
    except (EmailComposerSettingsError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="configure-email-signature",
        result="success",
        detail="Updated the shared email signature; signature content is not retained in the audit log.",
    )
    db.commit()
    return RedirectResponse(url="/settings?email_signature=saved#account-mailbox", status_code=303)


@router.post("/mcp-tokens")
def create_mcp_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    current_user = _require_admin_user(request)
    service = _account_service(db)
    user = service.get_user(current_user.id)
    if user is None:
        request.session.clear()
        return RedirectResponse(url="/account/login", status_code=303)

    try:
        access_token, raw_token = service.create_mcp_access_token(user=user, name=name)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="create-mcp-token",
        result="success",
        detail=f"Created MCP token {access_token.id} ({access_token.name}).",
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "account.html",
        _account_context(request, user, service, new_mcp_token=raw_token, new_mcp_token_name=access_token.name),
    )


@router.post("/mcp-tokens/{token_id}/revoke")
def revoke_mcp_token(
    token_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    current_user = _require_admin_user(request)
    service = _account_service(db)
    user = service.get_user(current_user.id)
    if user is None:
        request.session.clear()
        return RedirectResponse(url="/account/login", status_code=303)

    try:
        access_token = service.revoke_mcp_access_token(user=user, token_id=token_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc)),
            status_code=404,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="revoke-mcp-token",
        result="success",
        detail=f"Revoked MCP token {access_token.id} ({access_token.name}).",
    )
    db.commit()
    return RedirectResponse(url="/account?mcp_token=revoked", status_code=303)


@router.post("/integration-tokens")
def create_integration_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    current_user = _require_admin_user(request)
    service = _account_service(db)
    user = service.get_user(current_user.id)
    if user is None:
        request.session.clear()
        return RedirectResponse(url="/account/login", status_code=303)
    try:
        token, raw_token = service.create_integration_token(user=user, name=name)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="create-integration-token",
        result="success",
        detail=f"Created CallApp integration token {token.id} ({token.name}).",
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "account.html",
        _account_context(
            request,
            user,
            service,
            new_integration_token=raw_token,
            new_integration_token_name=token.name,
        ),
    )


@router.post("/integration-tokens/{token_id}/revoke")
def revoke_integration_token(
    token_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    current_user = _require_admin_user(request)
    service = _account_service(db)
    user = service.get_user(current_user.id)
    if user is None:
        request.session.clear()
        return RedirectResponse(url="/account/login", status_code=303)
    try:
        token = service.revoke_integration_token(user=user, token_id=token_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc)),
            status_code=404,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="revoke-integration-token",
        result="success",
        detail=f"Revoked CallApp integration token {token.id} ({token.name}).",
    )
    db.commit()
    return RedirectResponse(url="/account?integration_token=revoked#account-mcp", status_code=303)


@router.post("/desktop-devices")
def create_desktop_device(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    current_user = _require_current_user(request)
    service = _account_service(db)
    user = service.get_user(current_user.id)
    if user is None:
        request.session.clear()
        return RedirectResponse(url="/account/login", status_code=303)
    try:
        device, raw_token = service.create_desktop_device(user=user, name=name)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="pair-desktop-notifier",
        result="success",
        detail=f"Paired desktop notifier device {device.id} ({device.name}).",
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "account.html",
        _account_context(
            request,
            user,
            service,
            new_desktop_device_token=raw_token,
            new_desktop_device_name=device.name,
        ),
    )


@router.post("/desktop-devices/{device_id}/revoke")
def revoke_desktop_device(
    device_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    current_user = _require_current_user(request)
    service = _account_service(db)
    user = service.get_user(current_user.id)
    if user is None:
        request.session.clear()
        return RedirectResponse(url="/account/login", status_code=303)
    try:
        device = service.revoke_desktop_device(user=user, device_id=device_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc)),
            status_code=404,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="revoke-desktop-notifier",
        result="success",
        detail=f"Revoked desktop notifier device {device.id} ({device.name}).",
    )
    db.commit()
    return RedirectResponse(url="/account?desktop_device=revoked", status_code=303)


@router.post("/openai")
def configure_openai(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    api_key: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    provider_service = AiProviderConfigService(db=db, cipher=get_secret_cipher())
    try:
        config = provider_service.configure_openai(actor=user, api_key=api_key)
    except AiProviderConfigError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="configure-openai",
        result="success",
        detail=f"Configured encrypted OpenAI access with model {config.model}.",
    )
    db.commit()
    return RedirectResponse(url="/account?openai=configured", status_code=303)


@router.post("/openai/model")
def select_openai_model(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    model: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            "settings.ai_model.update", {"model": model})
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(request, "account.html",
            _account_context(request, user, _account_service(db), error=str(exc), error_section="account-openai"),
            status_code=400)
    db.commit()
    return RedirectResponse(url="/account?openai=model-saved#account-openai", status_code=303)


@router.post("/openai/remove")
def remove_openai(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    provider_service = AiProviderConfigService(db=db, cipher=get_secret_cipher())
    try:
        provider_service.remove_openai(actor=user)
    except AiProviderConfigError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="remove-openai",
        result="success",
        detail="Removed encrypted OpenAI access.",
    )
    db.commit()
    return RedirectResponse(url="/account?openai=removed", status_code=303)


@router.post("/crocoblock")
def configure_crocoblock(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    license_key: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = CrocoblockLicenseService(db=db, cipher=get_secret_cipher())
    try:
        service.configure(actor=user, license_key=license_key)
    except CrocoblockLicenseError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="configure-crocoblock-license",
        result="success",
        detail="Configured an encrypted Crocoblock license. The license key was not logged.",
    )
    db.commit()
    return RedirectResponse(url="/account?crocoblock=configured", status_code=303)


@router.post("/crocoblock/remove")
def remove_crocoblock(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = CrocoblockLicenseService(db=db, cipher=get_secret_cipher())
    try:
        service.remove(actor=user)
    except CrocoblockLicenseError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="remove-crocoblock-license",
        result="success",
        detail="Removed the centrally stored Crocoblock license.",
    )
    db.commit()
    return RedirectResponse(url="/account?crocoblock=removed", status_code=303)


@router.post("/fleet-refresh-settings")
def configure_fleet_refresh_settings(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    max_parallel_site_checks: Annotated[int, Form()] = 5,
    max_parallel_direct_updates: Annotated[int, Form()] = 5,
    auto_refresh_enabled: Annotated[bool, Form()] = False,
    auto_refresh_interval_hours: Annotated[int, Form()] = 24,
    auto_refresh_time: Annotated[str, Form()] = "03:00",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = FleetRefreshSettingsService(db=db)
    try:
        config = _administration(db, user).configure("fleet_refresh",
            max_parallel_site_checks=max_parallel_site_checks,
            max_parallel_direct_updates=max_parallel_direct_updates,
            auto_refresh_enabled=auto_refresh_enabled,
            auto_refresh_interval_hours=auto_refresh_interval_hours,
            auto_refresh_time=auto_refresh_time,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="configure-fleet-refresh-settings",
        result="success",
        detail=(
            f"Set parallel site checks to {config.max_parallel_site_checks} and parallel direct updates to "
            f"{config.max_parallel_direct_updates}. Automatic refresh is "
            f"{'enabled' if config.auto_refresh_enabled else 'disabled'} at "
            f"{config.auto_refresh_time} Europe/Berlin every {config.auto_refresh_interval_hours} hours."
        ),
    )
    db.commit()
    return RedirectResponse(url="/settings?fleet_refresh=settings-saved#account-refresh-settings", status_code=303)


@router.post("/provider-licenses")
def configure_provider_license(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider: Annotated[str, Form()] = "",
    license_key: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = ProviderCredentialService(db=db, cipher=get_secret_cipher())
    try:
        credential = service.configure(actor=user, provider=provider, license_key=license_key)
    except ProviderCredentialError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="configure-provider-license",
        result="success",
        detail=f"Configured encrypted license credentials for provider {credential.provider}. The secret was not logged.",
    )
    db.commit()
    return RedirectResponse(url="/account?provider_license=configured", status_code=303)


@router.post("/provider-licenses/{provider}/remove")
def remove_provider_license(
    provider: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = ProviderCredentialService(db=db, cipher=get_secret_cipher())
    try:
        credential = service.remove(actor=user, provider=provider)
    except ProviderCredentialError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="hub-account",
        action="remove-provider-license",
        result="success",
        detail=f"Removed encrypted license credentials for provider {credential.provider}.",
    )
    db.commit()
    return RedirectResponse(url="/account?provider_license=removed", status_code=303)


@router.post("/zoho")
def configure_zoho(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    data_center: Annotated[str, Form()] = "eu",
    client_id: Annotated[str, Form()] = "",
    client_secret: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = _zoho_service(db)
    try:
        service.configure(actor=user, data_center=data_center, client_id=client_id, client_secret=client_secret)
    except ZohoCrmError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="configure-zoho-crm-connection",
        result="success",
        detail="Stored encrypted Zoho CRM OAuth client credentials. Full CRM OAuth access still requires explicit connection.",
    )
    db.commit()
    return RedirectResponse(url="/settings?zoho=configured#account-zoho", status_code=303)


@router.get("/zoho/connect")
def connect_zoho(request: Request, db: Annotated[Session, Depends(get_db)]):
    _require_admin_user(request)
    service = _zoho_service(db)
    state = service.new_oauth_state()
    request.session["zoho_oauth_state"] = state
    try:
        authorization_url = service.build_authorization_url(state=state)
    except ZohoCrmError as exc:
        service.record_error(str(exc))
        db.commit()
        return RedirectResponse(url="/settings?zoho=connect-failed#account-zoho", status_code=303)
    return RedirectResponse(url=authorization_url, status_code=303)


@router.get("/zoho-books/connect")
def connect_zoho_books(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = _require_admin_user(request)
    service = _zoho_books_service(db)
    state = service.new_oauth_state()
    try:
        service.prepare_authorization(actor=user)
        authorization_url = service.build_authorization_url(state=state)
        # The callback arrives in a later request, so its separate database
        # session must be able to load this pending Books connection.
        db.commit()
    except ZohoBooksError as exc:
        service.record_error(str(exc))
        db.commit()
        return RedirectResponse(url="/settings?zoho_books=connect-failed#account-zoho-books", status_code=303)
    request.session["zoho_books_oauth_state"] = state
    return RedirectResponse(url=authorization_url, status_code=303)


@router.get("/zoho/callback")
def zoho_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    code: str = "",
    state: str = "",
    error: str = "",
):
    user = _require_admin_user(request)
    expected_books_state = request.session.pop("zoho_books_oauth_state", "")
    if expected_books_state:
        service = _zoho_books_service(db)
        if not isinstance(expected_books_state, str) or not compare_digest(expected_books_state, state):
            service.record_error("Der Status der Zoho-Books-Verbindung passt nicht. Starte die Verbindung erneut.")
            db.commit()
            return RedirectResponse(url="/settings?zoho_books=connect-failed#account-zoho-books", status_code=303)
        if error:
            service.record_error("Der Zoho-Books-Zugriff wurde nicht freigegeben.")
            db.commit()
            return RedirectResponse(url="/settings?zoho_books=not-approved#account-zoho-books", status_code=303)
        try:
            organizations = service.complete_authorization(code=code)
            status = service.get_status()
        except ZohoBooksError as exc:
            service.record_error(str(exc))
            db.commit()
            return RedirectResponse(url="/settings?zoho_books=connect-failed#account-zoho-books", status_code=303)
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="zoho-books",
            action="connect-zoho-books",
            result="success",
            detail=(
                "Connected Zoho Books with read-only access for settings, items, contacts, quotes, invoices, "
                "recurring invoices, credit notes, customer payments, and sales orders. "
                f"{len(organizations)} accessible organization(s) were found."
            ),
        )
        db.commit()
        outcome = "connected" if status.ready_for_import else "organization-selection-required"
        return RedirectResponse(url=f"/settings?zoho_books={outcome}#account-zoho-books", status_code=303)

    expected_state = request.session.pop("zoho_oauth_state", "")
    if not isinstance(expected_state, str) or not expected_state or not compare_digest(expected_state, state):
        _zoho_service(db).record_error("The Zoho connection state did not match. Start the connection again.")
        db.commit()
        return RedirectResponse(url="/settings?zoho=connect-failed#account-zoho", status_code=303)
    if error:
        _zoho_service(db).record_error("Zoho access was not approved.")
        db.commit()
        return RedirectResponse(url="/settings?zoho=not-approved#account-zoho", status_code=303)

    service = _zoho_service(db)
    try:
        service.complete_authorization(code=code)
        mappings = service.refresh_field_mapping()
    except ZohoCrmError as exc:
        service.record_error(str(exc))
        db.commit()
        return RedirectResponse(url="/settings?zoho=connect-failed#account-zoho", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="connect-zoho-crm",
        result="success",
        detail=(
            "Connected Zoho CRM with the full configured scope and "
            f"{sum(row.api_name is not None for row in mappings)} mapped Account fields. "
            "The currently implemented Account sync remains read-only."
        ),
    )
    db.commit()
    return RedirectResponse(url="/settings?zoho=connected#account-zoho", status_code=303)


@router.post("/zoho-books/organization")
def select_zoho_books_organization(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    organization_id: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = _zoho_books_service(db)
    try:
        connection = service.select_organization(actor=user, organization_id=organization_id)
    except ZohoBooksError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc), error_section="account-zoho-books"),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-books",
        action="select-zoho-books-organization",
        result="success",
        detail=f"Selected Zoho Books organization {connection.organization_id} ({connection.organization_name}).",
    )
    db.commit()
    return RedirectResponse(url="/settings?zoho_books=organization-selected#account-zoho-books", status_code=303)


@router.post("/zoho-books/organizations/refresh")
def refresh_zoho_books_organizations(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = _zoho_books_service(db)
    try:
        organizations = service.refresh_organizations()
    except ZohoBooksError as exc:
        service.record_error(str(exc))
        db.commit()
        return RedirectResponse(url="/settings?zoho_books=refresh-failed#account-zoho-books", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-books",
        action="refresh-zoho-books-organizations",
        result="success",
        detail=f"Refreshed {len(organizations)} accessible Zoho Books organization(s).",
    )
    db.commit()
    return RedirectResponse(url="/settings?zoho_books=organizations-refreshed#account-zoho-books", status_code=303)


@router.post("/zoho-books/invoices/import")
def import_recent_zoho_books_invoices(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        status, started = ZohoBooksInvoiceImportService(
            db=db,
            cipher=get_secret_cipher(),
        ).start(requested_by=user.username, limit=100)
    except (FinanceInvoicePdfStorageError, ValueError, ZohoBooksError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc), error_section="account-zoho-books"),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-books",
        action="import-recent-zoho-books-invoices",
        result="started" if started else "already-running",
        detail=(
            f"Zoho Books invoice import {status.id} {'started' if started else 'was already active'} "
            f"for {status.total_invoices} recent invoice(s)."
        ),
    )
    db.commit()
    schedule_pending_zoho_books_invoice_import()
    state = "invoice-import-started" if started else "invoice-import-running"
    return RedirectResponse(url=f"/settings?zoho_books={state}#account-zoho-books", status_code=303)


@router.post("/zoho-books/invoices/import/remaining")
def import_remaining_zoho_books_invoices(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        status, started = ZohoBooksInvoiceImportService(
            db=db,
            cipher=get_secret_cipher(),
        ).start_remaining(requested_by=user.username)
    except (FinanceInvoicePdfStorageError, ValueError, ZohoBooksError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc), error_section="account-zoho-books"),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-books",
        action="import-remaining-zoho-books-invoices",
        result="started" if started else "already-running",
        detail=(
            f"Zoho Books invoice import {status.id} {'started' if started else 'was already active'} "
            f"for {status.total_invoices} remaining invoice(s)."
        ),
    )
    db.commit()
    schedule_pending_zoho_books_invoice_import()
    state = "invoice-remaining-import-started" if started else "invoice-import-running"
    return RedirectResponse(url=f"/settings?zoho_books={state}#account-zoho-books", status_code=303)


@router.post("/zoho-books/invoices/import/cancel")
def cancel_zoho_books_invoice_import(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    status, requested = ZohoBooksInvoiceImportService(db=db, cipher=get_secret_cipher()).cancel()
    if requested and status is not None:
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="zoho-books",
            action="cancel-zoho-books-invoice-import",
            result="requested",
            detail=f"Cancellation requested for Zoho Books invoice import {status.id}.",
        )
        db.commit()
    state = "invoice-import-cancel-requested" if requested else "invoice-import-not-running"
    return RedirectResponse(url=f"/settings?zoho_books={state}#account-zoho-books", status_code=303)


@router.get("/zoho-books/invoices/import/status")
def zoho_books_invoice_import_status(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_admin_user(request)
    status = ZohoBooksInvoiceImportService(db=db, cipher=get_secret_cipher()).status()
    if status is None:
        return JSONResponse({"active": False, "status": None})
    return JSONResponse(
        {
            "active": status.status in {"pending", "running"},
            "status": status.status,
            "processed_invoices": status.processed_invoices,
            "total_invoices": status.total_invoices,
            "imported_invoices": status.imported_invoices,
            "updated_invoices": status.updated_invoices,
            "stored_pdfs": status.stored_pdfs,
            "unavailable_pdfs": status.unavailable_pdfs,
            "failed_invoices": status.failed_invoices,
            "consecutive_failures": status.consecutive_failures,
            "cancel_requested": status.cancel_requested,
            "last_error": status.last_error,
        }
    )


@router.post("/zoho-books/orders/import")
def import_all_zoho_orders(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        status, started = ZohoBooksOrderImportService(
            db=db,
            cipher=get_secret_cipher(),
        ).start_all(requested_by=user.username)
    except (ValueError, ZohoBooksError, ZohoCrmError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(
                request,
                user,
                _account_service(db),
                error=str(exc),
                error_section="account-zoho-books",
            ),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho",
        action="import-all-zoho-orders",
        result="started" if started else "already-running",
        detail=(
            f"Combined Zoho order import {status.id} "
            f"{'started' if started else 'was already active'} for {status.total_orders} order(s)."
        ),
    )
    db.commit()
    schedule_pending_zoho_books_order_import()
    state = "order-import-started" if started else "order-import-running"
    return RedirectResponse(url=f"/settings?zoho_books={state}#account-zoho-books", status_code=303)


@router.post("/zoho-books/orders/import/cancel")
def cancel_zoho_order_import(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    status, requested = ZohoBooksOrderImportService(
        db=db,
        cipher=get_secret_cipher(),
    ).cancel()
    if requested and status is not None:
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="zoho",
            action="cancel-zoho-order-import",
            result="requested",
            detail=f"Cancellation requested for combined Zoho order import {status.id}.",
        )
        db.commit()
    state = "order-import-cancel-requested" if requested else "order-import-not-running"
    return RedirectResponse(url=f"/settings?zoho_books={state}#account-zoho-books", status_code=303)


@router.get("/zoho-books/orders/import/status")
def zoho_order_import_status(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_admin_user(request)
    status = ZohoBooksOrderImportService(db=db, cipher=get_secret_cipher()).status()
    if status is None:
        return JSONResponse({"active": False, "status": None})
    return JSONResponse(
        {
            "active": status.status in {"pending", "running"},
            "status": status.status,
            "processed_orders": status.processed_orders,
            "total_orders": status.total_orders,
            "imported_orders": status.imported_orders,
            "updated_orders": status.updated_orders,
            "crm_matched_orders": status.crm_matched_orders,
            "crm_unmatched_orders": status.crm_unmatched_orders,
            "crm_ambiguous_orders": status.crm_ambiguous_orders,
            "failed_orders": status.failed_orders,
            "consecutive_failures": status.consecutive_failures,
            "cancel_requested": status.cancel_requested,
            "last_error": status.last_error,
        }
    )


@router.post("/zoho-books/recurring-invoices/import")
def import_all_zoho_books_recurring_invoices(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        status, started = ZohoBooksRecurringInvoiceImportService(
            db=db,
            cipher=get_secret_cipher(),
        ).start_all(requested_by=user.username)
    except (ValueError, ZohoBooksError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(
                request,
                user,
                _account_service(db),
                error=str(exc),
                error_section="account-zoho-books",
            ),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-books",
        action="import-all-zoho-books-recurring-invoices",
        result="started" if started else "already-running",
        detail=(
            f"Zoho Books recurring invoice import {status.id} "
            f"{'started' if started else 'was already active'} for {status.total_invoices} profile(s)."
        ),
    )
    db.commit()
    schedule_pending_zoho_books_recurring_invoice_import()
    state = "recurring-invoice-import-started" if started else "recurring-invoice-import-running"
    return RedirectResponse(url=f"/settings?zoho_books={state}#account-zoho-books", status_code=303)


@router.post("/zoho-books/recurring-invoices/import/cancel")
def cancel_zoho_books_recurring_invoice_import(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    status, requested = ZohoBooksRecurringInvoiceImportService(
        db=db,
        cipher=get_secret_cipher(),
    ).cancel()
    if requested and status is not None:
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="zoho-books",
            action="cancel-zoho-books-recurring-invoice-import",
            result="requested",
            detail=f"Cancellation requested for Zoho Books recurring invoice import {status.id}.",
        )
        db.commit()
    state = "recurring-invoice-import-cancel-requested" if requested else "recurring-invoice-import-not-running"
    return RedirectResponse(url=f"/settings?zoho_books={state}#account-zoho-books", status_code=303)


@router.get("/zoho-books/recurring-invoices/import/status")
def zoho_books_recurring_invoice_import_status(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_admin_user(request)
    status = ZohoBooksRecurringInvoiceImportService(db=db, cipher=get_secret_cipher()).status()
    if status is None:
        return JSONResponse({"active": False, "status": None})
    return JSONResponse(
        {
            "active": status.status in {"pending", "running"},
            "status": status.status,
            "processed_invoices": status.processed_invoices,
            "total_invoices": status.total_invoices,
            "imported_invoices": status.imported_invoices,
            "updated_invoices": status.updated_invoices,
            "failed_invoices": status.failed_invoices,
            "consecutive_failures": status.consecutive_failures,
            "cancel_requested": status.cancel_requested,
            "last_error": status.last_error,
        }
    )


@router.post("/zoho/mapping")
def refresh_zoho_mapping(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = _zoho_service(db)
    try:
        mappings = service.refresh_field_mapping()
    except ZohoCrmError as exc:
        service.record_error(str(exc))
        db.commit()
        return RedirectResponse(url="/settings?zoho=mapping-failed#account-zoho", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="refresh-accounts-field-mapping",
        result="success",
        detail=f"Refreshed {sum(row.api_name is not None for row in mappings)} Zoho Account field mappings.",
    )
    db.commit()
    return RedirectResponse(url="/settings?zoho=mapping-refreshed#account-zoho", status_code=303)


@router.post("/zoho/sync")
def sync_zoho_accounts(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = _zoho_service(db)
    try:
        result = service.sync_accounts()
    except ZohoCrmError as exc:
        service.record_error(str(exc))
        db.commit()
        return RedirectResponse(url="/settings?zoho=sync-failed#account-zoho", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="sync-customers-and-contacts-read-only",
        result="success",
        detail=(
            f"Synchronized {result.synchronized_accounts} Zoho Accounts and {result.synchronized_contacts} linked Contacts: created {result.created_customers}, "
            f"updated {result.updated_customers}, {result.visible_accounts} visible, and {result.hidden_customers} hidden; "
            f"created {result.created_sites} managed site records, linked {result.linked_sites} existing site records, "
            f"and found {result.site_conflicts} site conflicts. Contacts: created {result.created_contacts}, "
            f"updated {result.updated_contacts}, and removed {result.removed_contacts}."
        ),
    )
    db.commit()
    return RedirectResponse(
        url=(
            f"/settings?zoho=synced&zoho_created={result.created_customers}&zoho_updated={result.updated_customers}"
            f"&zoho_total={result.synchronized_accounts}&zoho_visible={result.visible_accounts}"
            f"&zoho_hidden={result.hidden_customers}&zoho_sites_created={result.created_sites}"
            f"&zoho_sites_linked={result.linked_sites}&zoho_site_conflicts={result.site_conflicts}"
            f"&zoho_contacts_total={result.synchronized_contacts}&zoho_contacts_created={result.created_contacts}"
            f"&zoho_contacts_updated={result.updated_contacts}&zoho_contacts_removed={result.removed_contacts}#account-zoho"
        ),
        status_code=303,
    )


@router.post("/zoho/email-history/import")
def import_zoho_email_history(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        status, started = ZohoEmailHistoryImportService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).start(requested_by=user.username)
    except (ValueError, ZohoCrmError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="import-all-zoho-email-headers",
        result="started" if started else "already-running",
        detail=(
            f"Zoho email header import {status.id} {'started' if started else 'was already active'} "
            f"for {status.total_customers} customers."
        ),
    )
    db.commit()
    schedule_pending_zoho_email_history_import()
    state = "email-history-import-started" if started else "email-history-import-running"
    return RedirectResponse(url=f"/settings?zoho={state}#account-zoho", status_code=303)


@router.post("/zoho/note-history/import")
def import_zoho_note_history(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        status, started = ZohoNoteHistoryImportService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).start(requested_by=user.username)
    except (ValueError, ZohoCrmError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="import-all-zoho-notes",
        result="started" if started else "already-running",
        detail=(
            f"Zoho note history import {status.id} {'started' if started else 'was already active'} "
            f"for {status.total_customers} customers."
        ),
    )
    db.commit()
    schedule_pending_zoho_note_history_import()
    state = "note-history-import-started" if started else "note-history-import-running"
    return RedirectResponse(url=f"/settings?zoho={state}#account-zoho", status_code=303)


@router.get("/zoho/note-history/import/status")
def zoho_note_history_import_status(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_admin_user(request)
    status = ZohoNoteHistoryImportService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    ).status()
    if status is None:
        return JSONResponse({"active": False, "status": None})
    return JSONResponse(
        {
            "active": status.status in {"pending", "running"},
            "status": status.status,
            "processed_customers": status.processed_customers,
            "total_customers": status.total_customers,
            "imported_notes": status.imported_notes,
            "last_error": status.last_error,
        }
    )


@router.post("/zoho/email-content/import")
@router.post("/zoho/email-content/import-test", include_in_schema=False)
def import_zoho_email_content_batch(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Form()] = 500,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        status, started = ZohoEmailContentImportService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).start(requested_by=user.username, limit=limit)
    except (ValueError, ZohoCrmError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="import-zoho-email-content-batch",
        result="started" if started else "already-running",
        detail=(
            f"Zoho email content import {status.id} {'started' if started else 'was already active'} "
            f"for {status.total_emails} selected email headers."
        ),
    )
    db.commit()
    schedule_pending_zoho_email_content_import()
    if started:
        state = "email-content-import-started"
    else:
        state = "email-content-import-running"
    return RedirectResponse(url=f"/settings?zoho={state}#account-zoho", status_code=303)


@router.post("/zoho/email-content/import/cancel")
def cancel_zoho_email_content_batch(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    status, requested = ZohoEmailContentImportService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    ).cancel()
    if requested and status is not None:
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="zoho-crm",
            action="cancel-zoho-email-content-batch",
            result="requested",
            detail=f"Cancellation requested for Zoho email content import {status.id}.",
        )
        db.commit()
    return RedirectResponse(url="/settings?zoho=email-content-import-cancel-requested#account-zoho", status_code=303)


@router.post("/zoho/email-attachments/import")
def import_zoho_email_attachments(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Form()] = 100,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        status, started = ZohoEmailAttachmentImportService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).start(requested_by=user.username, limit=limit)
    except (EmailAttachmentStorageError, ValueError, ZohoCrmError) as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )

    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="import-zoho-email-attachments",
        result="started" if started else "already-running",
        detail=(
            f"Zoho email attachment import {status.id} {'started' if started else 'was already active'} "
            f"for {status.total_attachments} attachment(s)."
        ),
    )
    db.commit()
    schedule_pending_zoho_email_attachment_import()
    state = "email-attachment-import-started" if started else "email-attachment-import-running"
    return RedirectResponse(url=f"/settings?zoho={state}#account-zoho", status_code=303)


@router.post("/zoho/email-attachments/import/cancel")
def cancel_zoho_email_attachment_import(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    status, requested = ZohoEmailAttachmentImportService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    ).cancel()
    if requested and status is not None:
        write_audit_log(
            db,
            site=None,
            actor=user.username,
            source="zoho-crm",
            action="cancel-zoho-email-attachment-import",
            result="requested",
            detail=f"Cancellation requested for Zoho email attachment import {status.id}.",
        )
        db.commit()
    return RedirectResponse(url="/settings?zoho=email-attachment-import-cancel-requested#account-zoho", status_code=303)


@router.get("/zoho/email-content/import/status")
def zoho_email_content_batch_status(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_admin_user(request)
    status = ZohoEmailContentImportService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    ).status()
    if status is None:
        return JSONResponse({"active": False, "status": None})
    return JSONResponse(
        {
            "active": status.status in {"pending", "running"},
            "status": status.status,
            "processed_emails": status.processed_emails,
            "total_emails": status.total_emails,
            "loaded_emails": status.loaded_emails,
            "failed_emails": status.failed_emails,
            "consecutive_failures": status.consecutive_failures,
            "cancel_requested": status.cancel_requested,
            "continue_automatically": status.continue_automatically,
            "last_error": status.last_error,
        }
    )


@router.get("/zoho/email-attachments/import/status")
def zoho_email_attachment_import_status(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    _require_admin_user(request)
    status = ZohoEmailAttachmentImportService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    ).status()
    if status is None:
        return JSONResponse({"active": False, "status": None})
    return JSONResponse(
        {
            "active": status.status in {"pending", "running"},
            "status": status.status,
            "processed_attachments": status.processed_attachments,
            "total_attachments": status.total_attachments,
            "stored_attachments": status.stored_attachments,
            "failed_attachments": status.failed_attachments,
            "stored_bytes": status.stored_bytes,
            "consecutive_failures": status.consecutive_failures,
            "cancel_requested": status.cancel_requested,
            "continue_automatically": status.continue_automatically,
            "last_error": status.last_error,
        }
    )


@router.post("/zoho/email-templates/sync")
def sync_zoho_email_templates(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    try:
        result = CustomerCommunicationService(
            db=db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).sync_email_templates()
    except (ValueError, ZohoCrmError) as exc:
        _zoho_service(db).record_error(str(exc))
        db.commit()
        return RedirectResponse(url="/settings?zoho=email-templates-failed#account-zoho", status_code=303)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="sync-zoho-email-templates-read-only",
        result="success",
        detail=f"Synchronized Zoho Account email templates: {result.created} created, {result.updated} updated, {result.archived} archived.",
    )
    db.commit()
    return RedirectResponse(
        url=f"/settings?zoho=email-templates-synced&zoho_templates_created={result.created}&zoho_templates_updated={result.updated}&zoho_templates_archived={result.archived}#account-zoho",
        status_code=303,
    )


@router.post("/zoho/email-workflow-webhook/token", response_class=HTMLResponse)
def rotate_zoho_email_workflow_webhook_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    token = _zoho_email_workflow_webhook_service(db).rotate_token()
    webhook_url = _zoho_email_workflow_webhook_service(db).endpoint_for_token(token)
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="rotate-email-workflow-webhook-token",
        result="success",
        detail="Rotated the token for the manually configured Zoho E-Mails webhook.",
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "account.html",
        _account_context(
            request,
            user,
            _account_service(db),
            new_zoho_email_workflow_webhook_url=webhook_url,
        ),
    )


@router.post("/zoho/email-workflow-webhook/receive/{token}")
async def receive_zoho_email_workflow_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: str,
):
    """Receive a manually configured Zoho E-Mails workflow webhook."""
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 64_000:
        raise HTTPException(status_code=413, detail="Zoho webhook payload is too large.")

    content_type = request.headers.get("content-type", "").casefold()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid Zoho webhook JSON payload.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Zoho webhook payload must be a JSON object.")
    else:
        form = await request.form()
        payload = {key: str(value) for key, value in form.multi_items() if isinstance(value, str)}
        if not payload:
            raise HTTPException(status_code=400, detail="Zoho webhook form data is empty.")

    service = _zoho_email_workflow_webhook_service(db)
    if not service.receive(token=token, payload=payload):
        raise HTTPException(status_code=403, detail="Unknown Zoho email workflow webhook URL.")
    db.commit()
    schedule_pending_zoho_email_workflow_deliveries()
    return Response(status_code=204)


@router.post("/zoho/remove")
def remove_zoho_connection(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = _zoho_service(db)
    try:
        service.remove_connection(actor=user)
    except ZohoCrmError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc)),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-crm",
        action="remove-connection",
        result="success",
        detail="Removed the encrypted Zoho OAuth credentials. Imported customer data remains until a separate customer-data action is added.",
    )
    db.commit()
    return RedirectResponse(url="/settings?zoho=removed#account-zoho", status_code=303)


@router.post("/zoho-books/remove")
def remove_zoho_books_connection(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_admin_user(request)
    service = _zoho_books_service(db)
    try:
        service.remove_connection(actor=user)
    except ZohoBooksError as exc:
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, _account_service(db), error=str(exc), error_section="account-zoho-books"),
            status_code=400,
        )
    write_audit_log(
        db,
        site=None,
        actor=user.username,
        source="zoho-books",
        action="remove-zoho-books-connection",
        result="success",
        detail="Removed the encrypted Zoho Books OAuth token and copied client credentials. Zoho CRM remains connected.",
    )
    db.commit()
    return RedirectResponse(url="/settings?zoho_books=removed#account-zoho-books", status_code=303)


@bootstrap_router.post("/internal/bootstrap-token")
def create_bootstrap_token(request: Request, db: Annotated[Session, Depends(get_db)]):
    # This endpoint is only reachable from an SSH shell on the Hub host, never through the public proxy.
    if not _is_direct_local_request(request):
        raise HTTPException(status_code=404, detail="Not found.")
    service = _account_service(db)
    try:
        token = service.create_setup_token()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"setup_url": f"{get_settings().public_base_url}/account/setup#token={token}", "expires_in_minutes": 20})


def _administration(db, user):
    return HubAdministrationService(db=db, cipher=get_secret_cipher(), actor=user.username)


def _account_service(db: Session) -> HubAccountService:
    return HubAccountService(db=db, app_secret_key=get_settings().app_secret_key)


def _zoho_service(db: Session) -> ZohoCrmService:
    return ZohoCrmService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )


def _zoho_books_service(db: Session) -> ZohoBooksService:
    return ZohoBooksService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )


def _zoho_email_workflow_webhook_service(db: Session) -> ZohoEmailWorkflowWebhookService:
    return ZohoEmailWorkflowWebhookService(
        db=db,
        cipher=get_secret_cipher(),
        public_base_url=get_settings().public_base_url,
    )


def _account_context(
    request: Request,
    user,
    service: HubAccountService,
    *,
    error: str | None = None,
    error_section: str | None = None,
    new_mcp_token: str | None = None,
    new_mcp_token_name: str | None = None,
    new_integration_token: str | None = None,
    new_integration_token_name: str | None = None,
    new_desktop_device_token: str | None = None,
    new_desktop_device_name: str | None = None,
    new_zoho_email_workflow_webhook_url: str | None = None,
    selected_legal_terms_id: int | None = None,
    selected_pdf_template_id: int | None = None,
    selected_pdf_template_type: str | None = None,
    page_mode: str | None = None,
) -> dict:
    resolved_error_section = error_section or _account_section_for_path(request.url.path)
    settings_sections = {
        "account-access",
        "account-workflows",
        "account-legal-terms",
        "account-pdf-templates",
        "account-mailbox",
        "account-zoho",
        "account-zoho-books",
        "account-refresh-settings",
        "account-styling",
    }
    resolved_page_mode = page_mode or (
        "settings"
        if request.url.path == "/settings" or resolved_error_section in settings_sections
        else "account"
    )
    if user.role != "admin":
        if resolved_page_mode == "settings":
            raise HTTPException(status_code=403, detail="Administrator access required.")
        # Personal account rendering must not load system credentials or administration data.
        return {
            "page_mode": "account",
            "user": user,
            "csrf_token": get_csrf_token(request),
            "desktop_devices": service.list_desktop_devices(user=user),
            "new_desktop_device_token": new_desktop_device_token,
            "new_desktop_device_name": new_desktop_device_name,
            "error": error,
            "error_section": resolved_error_section if resolved_error_section in {
                "account-security", "account-desktop-notifier",
            } else "account-security",
        }
    protocol = list_activity_events(service.db, request.query_params) if user.role == "admin" else None
    if protocol is not None:
        filters = {key: value for key, value in request.query_params.items() if key.startswith("protocol_") and key != "protocol_page"}
        protocol["previous_url"] = (
            f"/account?{urlencode({**filters, 'protocol_page': protocol['page'] - 1})}#account-protocol"
            if protocol["page"] > 1 else None
        )
        protocol["next_url"] = (
            f"/account?{urlencode({**filters, 'protocol_page': protocol['page'] + 1})}#account-protocol"
            if protocol["has_next"] else None
        )
    zoho_service = _zoho_service(service.db)
    zoho_books_service = _zoho_books_service(service.db)
    legal_terms_context = _legal_terms_context(
        request,
        user,
        HubLegalTermsService(db=service.db),
        selected_legal_terms_id=selected_legal_terms_id,
    )
    pdf_template_context = _pdf_template_context(
        request,
        user,
        HubPdfTemplateService(db=service.db),
        selected_template_id=selected_pdf_template_id,
        selected_type=selected_pdf_template_type,
    )
    access_roles = ()
    access_teams = ()
    access_permissions: dict[str, dict[str, object]] = {}
    access_assignments = ()
    access_grants = ()
    if user.role == "admin":
        access_service = HubAccessControlService(db=service.db)
        access_service.ensure_defaults()
        snapshot = _administration(service.db, user).access_snapshot()
        access_roles = snapshot["roles"]
        access_teams = snapshot["teams"]
        access_permissions = snapshot["permissions"]
        access_assignments = snapshot["assignments"]
        access_grants = snapshot["grants"]
    role_labels = {role.key: role.name for role in access_roles}
    team_labels = {team.id: team.name for team in access_teams}
    access_users = snapshot["users"] if user.role == "admin" else ()
    return {
        "page_mode": resolved_page_mode,
        "user": user,
        "hub_users": access_users,
        "hub_admin_count": service.admin_count() if user.role == "admin" else 0,
        "hub_user_roles": tuple(role.key for role in access_roles) or HUB_USER_ROLES,
        "hub_role_labels": role_labels or {"admin": "Administrator", "viewer": "Mitarbeiter"},
        "hub_teams": access_teams,
        "hub_team_labels": team_labels,
        "access_roles": access_roles,
        "access_permissions": access_permissions,
        "mailbox_access": _administration(service.db, user).mailbox_access_snapshot() if user.role == "admin" else {},
        "mailbox_action_labels": {"view": "Lesen", "create": "Entwürfe anlegen", "edit": "Bearbeiten", "send": "Senden", "delete": "Löschen"},
        "access_modules": ACCESS_MODULES,
        "access_actions": ACCESS_ACTIONS,
        "access_action_labels": ACCESS_ACTION_LABELS,
        "access_scope_labels": ACCESS_SCOPE_LABELS,
        "access_record_modules": tuple(module for module in ACCESS_MODULES if module.record_scoped),
        "access_assignments": access_assignments,
        "access_grants": access_grants,
        "access_user_labels": {account_user.id: account_user.display_name for account_user in access_users},
        "protocol": protocol,
        "protocol_module_labels": protocol["modules"] if protocol is not None else MODULE_LABELS,
        "csrf_token": get_csrf_token(request),
        "mcp_tokens": service.list_mcp_access_tokens(user=user),
        "integration_tokens": service.list_integration_tokens(user=user),
        "desktop_devices": service.list_desktop_devices(user=user),
        "error": error,
        "error_section": resolved_error_section,
        "new_mcp_token": new_mcp_token,
        "new_mcp_token_name": new_mcp_token_name,
        "new_integration_token": new_integration_token,
        "new_integration_token_name": new_integration_token_name,
        "new_desktop_device_token": new_desktop_device_token,
        "new_desktop_device_name": new_desktop_device_name,
        "zoho_email_workflow_webhook": _zoho_email_workflow_webhook_service(service.db).status(),
        "zoho_email_history_import": ZohoEmailHistoryImportService(
            db=service.db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).status(),
        "zoho_note_history_import": ZohoNoteHistoryImportService(
            db=service.db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).status(),
        "zoho_email_content_import": ZohoEmailContentImportService(
            db=service.db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).status(),
        "zoho_email_attachment_import": ZohoEmailAttachmentImportService(
            db=service.db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).status(),
        "new_zoho_email_workflow_webhook_url": new_zoho_email_workflow_webhook_url,
        "openai_config": AiProviderConfigService(db=service.db, cipher=get_secret_cipher()).get_openai_config(),
        "openai_models": MODEL_PROFILES,
        "provider_licenses": ProviderCredentialService(db=service.db, cipher=get_secret_cipher()).list_rows(),
        "fleet_refresh_settings": runtime_settings(service.db, "fleet_refresh"),
        "mittwald_mailbox_accounts": HubMailboxAccountService(
            db=service.db,
            cipher=get_secret_cipher(),
        ).list_statuses(),
        "mittwald_mailbox_sync_failures": HubMailboxImapSyncService(
            db=service.db, cipher=get_secret_cipher(), public_base_url=get_settings().public_base_url,
        ).list_failed_messages(),
        "spam_senders": HubSpamSenderService(db=service.db).list_senders() if user.role == "admin" else (),
        "mittwald_mailbox_import": HubMailboxImapImportService(
            db=service.db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).status(),
        "email_composer_settings": runtime_settings(service.db, "email_composer"),
        "signature_placeholders": USER_PLACEHOLDERS,
        "email_composer_font_options": FONT_FAMILY_OPTIONS,
        "email_composer_font_size_options": FONT_SIZE_OPTIONS,
        "email_composer_line_height_options": LINE_HEIGHT_OPTIONS,
        "styling": runtime_settings(service.db, "styling"),
        "font_family_options": [
            {"key": key, "label": label}
            for key, label, _ in STYLING_FONT_FAMILY_OPTIONS
        ],
        "workflows": HubWorkflowService(db=service.db).list_workflows(),
        "zoho_status": zoho_service.get_status(),
        "zoho_mapping": zoho_service.mapping_rows(),
        "zoho_data_centers": ZOHO_DATA_CENTERS.values(),
        "zoho_books_status": zoho_books_service.get_status(),
        "zoho_books_invoice_import": ZohoBooksInvoiceImportService(
            db=service.db,
            cipher=get_secret_cipher(),
        ).status(),
        "zoho_books_order_import": ZohoBooksOrderImportService(
            db=service.db,
            cipher=get_secret_cipher(),
        ).status(),
        "zoho_books_recurring_invoice_import": ZohoBooksRecurringInvoiceImportService(
            db=service.db,
            cipher=get_secret_cipher(),
        ).status(),
        **legal_terms_context,
        **pdf_template_context,
    }


def _current_user(request: Request):
    return getattr(request.state, "hub_user", None)


def _require_current_user(request: Request):
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _require_persisted_current_user(request: Request, service: HubAccountService):
    """Use the request identity to load the writable user in this request's session."""
    current_user = _require_current_user(request)
    user = service.get_user(current_user.id)
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _require_admin_user(request: Request):
    user = _require_current_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else ""


def _account_section_for_path(path: str) -> str:
    if path.startswith("/account/legal-terms"):
        return "account-legal-terms"
    if path.startswith("/account/pdf-templates"):
        return "account-pdf-templates"
    if path.startswith("/account/mail-composer-settings"):
        return "account-mailbox"
    if path.startswith("/account/mail-signature"):
        return "account-mailbox"
    if path.startswith("/account/mailboxes"):
        return "account-mailbox"
    if path.startswith("/account/desktop-devices"):
        return "account-desktop-notifier"
    if path.startswith("/account/mcp-tokens"):
        return "account-mcp"
    if path.startswith("/account/integration-tokens"):
        return "account-mcp"
    if path.startswith("/account/openai"):
        return "account-openai"
    if path.startswith("/account/crocoblock") or path.startswith("/account/provider-licenses"):
        return "account-provider-licenses"
    if path == "/account/fleet-refresh-settings":
        return "account-refresh-settings"
    if path.startswith("/account/zoho-books"):
        return "account-zoho-books"
    if path.startswith("/account/zoho"):
        return "account-zoho"
    return "account-security"


def _execute_document_template_operation(db, user, family, action, **values):
    encoded = {
        key: json.dumps(value) if isinstance(value, (list, dict, bool)) else str(value) if value is not None else ""
        for key, value in values.items()
    }
    domain = HubPdfTemplateService(db=db) if family == "pdf_templates" else HubLegalTermsService(db=db)
    try:
        result = HubOperationService(db=db, cipher=get_secret_cipher(), actor=user.username).execute(
            f"finance.{family}.{action}", encoded,
        )
    except HubOperationError as exc:
        error = HubPdfTemplateError if family == "pdf_templates" else HubLegalTermsError
        raise error(str(exc)) from exc
    return result.outputs.get("document_type", "") if action == "delete" else domain.get(result.record_id)


def _legal_terms_context(
    request: Request,
    user,
    service: HubLegalTermsService,
    *,
    selected_legal_terms_id: int | None = None,
) -> dict[str, object]:
    if user.role != "admin":
        return {"legal_terms": (), "selected_legal_terms": None}
    legal_terms = service.list_terms()
    query_id = request.query_params.get("legal_terms", "")
    if selected_legal_terms_id is None and query_id.isdigit():
        selected_legal_terms_id = int(query_id)
    selected = service.get(selected_legal_terms_id) if selected_legal_terms_id is not None else None
    if selected is None:
        selected = legal_terms[0]
    return {"legal_terms": legal_terms, "selected_legal_terms": selected}


def _pdf_template_context(
    request: Request,
    user,
    service: HubPdfTemplateService,
    *,
    selected_template_id: int | None = None,
    selected_type: str | None = None,
) -> dict[str, object]:
    if user.role != "admin":
        return {
            "pdf_template_types": (),
            "pdf_templates_by_type": {},
            "selected_pdf_template": None,
            "selected_pdf_template_type": "offers",
            "pdf_line_sources": (),
        }
    templates_by_type = {
        definition.key: service.list_templates(document_type=definition.key)
        for definition in PDF_TEMPLATE_TYPES
    }
    query_template_id = request.query_params.get("pdf_template", "")
    if selected_template_id is None and query_template_id.isdigit():
        selected_template_id = int(query_template_id)
    query_type = request.query_params.get("pdf_template_type", "")
    active_type = selected_type or query_type
    if active_type not in templates_by_type:
        active_type = "offers"
    selected = service.get(selected_template_id) if selected_template_id is not None else None
    if selected is not None:
        active_type = selected.document_type
    else:
        selected = next(
            (template for template in templates_by_type[active_type] if template.is_default),
            templates_by_type[active_type][0],
        )
    return {
        "pdf_template_types": PDF_TEMPLATE_TYPES,
        "pdf_templates_by_type": templates_by_type,
        "selected_pdf_template": service.editor_view(selected),
        "selected_pdf_template_type": active_type,
        "pdf_line_sources": PDF_LINE_SOURCES,
        "legal_terms": HubLegalTermsService(db=service.db).list_terms(),
    }


def _legal_terms_redirect(legal_terms_id: int | None, state: str) -> RedirectResponse:
    parameters = [f"legal_terms_state={state}"]
    if legal_terms_id is not None:
        parameters.append(f"legal_terms={legal_terms_id}")
    return RedirectResponse(url=f"/settings?{'&'.join(parameters)}#account-legal-terms", status_code=303)


def _legal_terms_error_response(
    request: Request,
    user,
    db: Session,
    message: str,
    *,
    selected_legal_terms_id: int | None = None,
):
    return templates.TemplateResponse(
        request,
        "account.html",
        _account_context(
            request,
            user,
            _account_service(db),
            error=message,
            error_section="account-legal-terms",
            selected_legal_terms_id=selected_legal_terms_id,
        ),
        status_code=400,
    )


def _pdf_template_redirect(template_id: int | None, document_type: str, state: str) -> RedirectResponse:
    parameters = [f"pdf_template_type={document_type}", f"pdf_template_state={state}"]
    if template_id is not None:
        parameters.append(f"pdf_template={template_id}")
    return RedirectResponse(url=f"/settings?{'&'.join(parameters)}#account-pdf-templates", status_code=303)


def _pdf_template_error_response(
    request: Request,
    user,
    db: Session,
    message: str,
    *,
    selected_template_id: int | None = None,
    selected_type: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "account.html",
        _account_context(
            request,
            user,
            _account_service(db),
            error=message,
            error_section="account-pdf-templates",
            selected_pdf_template_id=selected_template_id,
            selected_pdf_template_type=selected_type,
        ),
        status_code=400,
    )


def _audit_pdf_template(db: Session, *, actor: str, action: str, template) -> None:
    write_audit_log(
        db,
        site=None,
        actor=actor,
        source="hub-pdf-template",
        action=action,
        result="success",
        detail=(
            f"PDF template {template.id} ({template.document_type}, version {template.version}) "
            f"was changed: {template.name}."
        ),
    )


def _audit_legal_terms(db: Session, *, actor: str, action: str, legal_terms) -> None:
    write_audit_log(
        db,
        site=None,
        actor=actor,
        source="hub-legal-terms",
        action=action,
        result="success",
        detail=f"AGB {legal_terms.id} (Version {legal_terms.version}) wurde geändert: {legal_terms.name}.",
    )


def _is_direct_local_request(request: Request) -> bool:
    client_host = request.client.host if request.client is not None else ""
    return (
        client_host in {"127.0.0.1", "::1"}
        and not request.headers.get("x-forwarded-for")
        and not request.headers.get("x-real-ip")
    )
