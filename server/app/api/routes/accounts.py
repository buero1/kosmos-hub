from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.templating import Jinja2Templates

from app.core.config import get_settings
from app.core.security import get_secret_cipher
from app.db.session import get_db
from app.services.audit import write_audit_log
from app.services.ai_provider import AiProviderConfigError, AiProviderConfigService
from app.services.crocoblock_license import CrocoblockLicenseError, CrocoblockLicenseService
from app.services.fleet_refresh_settings import FleetRefreshSettingsError, FleetRefreshSettingsService
from app.services.hub_accounts import HubAccountService
from app.services.provider_credentials import ProviderCredentialError, ProviderCredentialService

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
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
    site_status_max_age_minutes: Annotated[int, Form()] = 15,
    official_version_max_age_hours: Annotated[int, Form()] = 24,
    max_parallel_site_checks: Annotated[int, Form()] = 5,
    max_parallel_direct_updates: Annotated[int, Form()] = 5,
    csrf_token: Annotated[str, Form()] = "",
):
    require_csrf(request, csrf_token)
    user = _require_current_user(request)
    service = FleetRefreshSettingsService(db=db)
    try:
        config = service.configure(
            actor=user,
            site_status_max_age_minutes=site_status_max_age_minutes,
            official_version_max_age_hours=official_version_max_age_hours,
            max_parallel_site_checks=max_parallel_site_checks,
            max_parallel_direct_updates=max_parallel_direct_updates,
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
            f"Set status cache to {config.site_status_max_age_minutes} minutes, official version cache to "
            f"{config.official_version_max_age_hours} hours, and parallel site checks to "
            f"{config.max_parallel_site_checks} and parallel direct updates to "
            f"{config.max_parallel_direct_updates}."
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


def _account_context(
    request: Request,
    user,
    service: HubAccountService,
    *,
    error: str | None = None,
    new_mcp_token: str | None = None,
    new_mcp_token_name: str | None = None,
) -> dict:
    return {
        "user": user,
        "csrf_token": get_csrf_token(request),
        "mcp_tokens": service.list_mcp_access_tokens(user=user),
        "error": error,
        "new_mcp_token": new_mcp_token,
        "new_mcp_token_name": new_mcp_token_name,
        "openai_config": AiProviderConfigService(db=service.db, cipher=get_secret_cipher()).get_openai_config(),
        "provider_licenses": ProviderCredentialService(db=service.db, cipher=get_secret_cipher()).list_rows(),
        "fleet_refresh_settings": FleetRefreshSettingsService(db=service.db).get_runtime_settings(),
    }


def _current_user(request: Request):
    return getattr(request.state, "hub_user", None)


def _require_current_user(request: Request):
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str):
        token = token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def require_csrf(request: Request, provided_token: str) -> None:
    expected_token = request.session.get("csrf_token")
    if not isinstance(expected_token, str) or not compare_digest(expected_token, provided_token):
        raise HTTPException(status_code=403, detail="Invalid form token.")


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else ""


def _is_direct_local_request(request: Request) -> bool:
    client_host = request.client.host if request.client is not None else ""
    return (
        client_host in {"127.0.0.1", "::1"}
        and not request.headers.get("x-forwarded-for")
        and not request.headers.get("x-real-ip")
    )
