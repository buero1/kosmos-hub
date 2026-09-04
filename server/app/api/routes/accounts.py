import json
from pathlib import Path
from secrets import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.csrf import get_csrf_token, require_csrf
from app.core.security import get_secret_cipher
from app.core.templates import create_templates
from app.db.session import get_db
from app.services.audit import write_audit_log
from app.services.ai_provider import AiProviderConfigError, AiProviderConfigService
from app.services.crocoblock_license import CrocoblockLicenseError, CrocoblockLicenseService
from app.services.fleet_refresh_settings import FleetRefreshSettingsError, FleetRefreshSettingsService
from app.services.hub_accounts import HubAccountService
from app.services.provider_credentials import ProviderCredentialError, ProviderCredentialService
from app.services.zoho_crm import ZOHO_DATA_CENTERS, ZohoCrmError, ZohoCrmService
from app.services.customer_communications import CustomerCommunicationService
from app.services.maintenance_worker import (
    schedule_pending_zoho_email_content_import,
    schedule_pending_zoho_email_history_import,
    schedule_pending_zoho_email_workflow_deliveries,
)
from app.services.zoho_email_history_import import ZohoEmailHistoryImportService
from app.services.zoho_email_content_import import ZohoEmailContentImportService
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
            {"next": _safe_next(next), "csrf_token": get_csrf_token(request), "error": "Username or password is incorrect."},
            status_code=400,
        )
    request.session.clear()
    request.session.update({"user_id": user.id, "session_version": user.session_version})
    return RedirectResponse(url=_safe_next(next) or "/", status_code=303)


@router.post("/logout")
def logout(request: Request, csrf_token: Annotated[str, Form()] = ""):
    require_csrf(request, csrf_token)
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
    user = _require_current_user(request)
    service = _account_service(db)
    return templates.TemplateResponse(request, "account.html", _account_context(request, user, service))


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
        return templates.TemplateResponse(
            request,
            "account.html",
            _account_context(request, user, service, error=str(exc)),
            status_code=400,
        )
    write_audit_log(db, site=None, actor=user.username, source="hub-account", action="change-password", result="success")
    db.commit()
    request.session.clear()
    request.session.update({"user_id": user.id, "session_version": user.session_version})
    return RedirectResponse(url="/account?password=changed", status_code=303)


@router.post("/mcp-tokens")
def create_mcp_token(
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
    current_user = _require_current_user(request)
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


@router.post("/openai")
def configure_openai(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    api_key: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_current_user(request)
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


@router.post("/openai/remove")
def remove_openai(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_current_user(request)
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
    user = _require_current_user(request)
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
    user = _require_current_user(request)
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
    user = _require_current_user(request)
    service = FleetRefreshSettingsService(db=db)
    try:
        config = service.configure(
            actor=user,
            max_parallel_site_checks=max_parallel_site_checks,
            max_parallel_direct_updates=max_parallel_direct_updates,
            auto_refresh_enabled=auto_refresh_enabled,
            auto_refresh_interval_hours=auto_refresh_interval_hours,
            auto_refresh_time=auto_refresh_time,
        )
    except FleetRefreshSettingsError as exc:
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
    return RedirectResponse(url="/account?fleet_refresh=settings-saved", status_code=303)


@router.post("/provider-licenses")
def configure_provider_license(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider: Annotated[str, Form()] = "",
    license_key: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_current_user(request)
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
    user = _require_current_user(request)
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
    return RedirectResponse(url="/account?zoho=configured", status_code=303)


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
        return RedirectResponse(url="/account?zoho=connect-failed", status_code=303)
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
    expected_state = request.session.pop("zoho_oauth_state", "")
    if not isinstance(expected_state, str) or not expected_state or not compare_digest(expected_state, state):
        _zoho_service(db).record_error("The Zoho connection state did not match. Start the connection again.")
        db.commit()
        return RedirectResponse(url="/account?zoho=connect-failed", status_code=303)
    if error:
        _zoho_service(db).record_error("Zoho access was not approved.")
        db.commit()
        return RedirectResponse(url="/account?zoho=not-approved", status_code=303)

    service = _zoho_service(db)
    try:
        service.complete_authorization(code=code)
        mappings = service.refresh_field_mapping()
    except ZohoCrmError as exc:
        service.record_error(str(exc))
        db.commit()
        return RedirectResponse(url="/account?zoho=connect-failed", status_code=303)
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
    return RedirectResponse(url="/account?zoho=connected", status_code=303)


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
        return RedirectResponse(url="/account?zoho=mapping-failed", status_code=303)
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
    return RedirectResponse(url="/account?zoho=mapping-refreshed", status_code=303)


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
        return RedirectResponse(url="/account?zoho=sync-failed", status_code=303)
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
            f"/account?zoho=synced&zoho_created={result.created_customers}&zoho_updated={result.updated_customers}"
            f"&zoho_total={result.synchronized_accounts}&zoho_visible={result.visible_accounts}"
            f"&zoho_hidden={result.hidden_customers}&zoho_sites_created={result.created_sites}"
            f"&zoho_sites_linked={result.linked_sites}&zoho_site_conflicts={result.site_conflicts}"
            f"&zoho_contacts_total={result.synchronized_contacts}&zoho_contacts_created={result.created_contacts}"
            f"&zoho_contacts_updated={result.updated_contacts}&zoho_contacts_removed={result.removed_contacts}"
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
    return RedirectResponse(url=f"/account?zoho={state}", status_code=303)


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
    return RedirectResponse(url=f"/account?zoho={state}", status_code=303)


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
    return RedirectResponse(url="/account?zoho=email-content-import-cancel-requested", status_code=303)


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
        return RedirectResponse(url="/account?zoho=email-templates-failed", status_code=303)
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
        url=f"/account?zoho=email-templates-synced&zoho_templates_created={result.created}&zoho_templates_updated={result.updated}&zoho_templates_archived={result.archived}",
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
    return RedirectResponse(url="/account?zoho=removed", status_code=303)


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


def _account_service(db: Session) -> HubAccountService:
    return HubAccountService(db=db, app_secret_key=get_settings().app_secret_key)


def _zoho_service(db: Session) -> ZohoCrmService:
    return ZohoCrmService(
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
    new_mcp_token: str | None = None,
    new_mcp_token_name: str | None = None,
    new_zoho_email_workflow_webhook_url: str | None = None,
) -> dict:
    zoho_service = _zoho_service(service.db)
    return {
        "user": user,
        "csrf_token": get_csrf_token(request),
        "mcp_tokens": service.list_mcp_access_tokens(user=user),
        "error": error,
        "error_section": _account_section_for_path(request.url.path),
        "new_mcp_token": new_mcp_token,
        "new_mcp_token_name": new_mcp_token_name,
        "zoho_email_workflow_webhook": _zoho_email_workflow_webhook_service(service.db).status(),
        "zoho_email_history_import": ZohoEmailHistoryImportService(
            db=service.db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).status(),
        "zoho_email_content_import": ZohoEmailContentImportService(
            db=service.db,
            cipher=get_secret_cipher(),
            public_base_url=get_settings().public_base_url,
        ).status(),
        "new_zoho_email_workflow_webhook_url": new_zoho_email_workflow_webhook_url,
        "openai_config": AiProviderConfigService(db=service.db, cipher=get_secret_cipher()).get_openai_config(),
        "provider_licenses": ProviderCredentialService(db=service.db, cipher=get_secret_cipher()).list_rows(),
        "fleet_refresh_settings": FleetRefreshSettingsService(db=service.db).get_runtime_settings(),
        "zoho_status": zoho_service.get_status(),
        "zoho_mapping": zoho_service.mapping_rows(),
        "zoho_data_centers": ZOHO_DATA_CENTERS.values(),
    }


def _current_user(request: Request):
    return getattr(request.state, "hub_user", None)


def _require_current_user(request: Request):
    user = _current_user(request)
    if user is None:
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
    if path.startswith("/account/mcp-tokens"):
        return "account-mcp"
    if path.startswith("/account/openai"):
        return "account-openai"
    if path.startswith("/account/crocoblock") or path.startswith("/account/provider-licenses"):
        return "account-provider-licenses"
    if path == "/account/fleet-refresh-settings":
        return "account-refresh-settings"
    if path.startswith("/account/zoho"):
        return "account-zoho"
    return "account-security"


def _is_direct_local_request(request: Request) -> bool:
    client_host = request.client.host if request.client is not None else ""
    return (
        client_host in {"127.0.0.1", "::1"}
        and not request.headers.get("x-forwarded-for")
        and not request.headers.get("x-real-ip")
    )
