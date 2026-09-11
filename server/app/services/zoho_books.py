"""Read-only Zoho Books OAuth connection and organization selection."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_user import HubUser
from app.models.zoho_books_connection import ZohoBooksConnection
from app.models.zoho_connection import ZohoConnection
from app.services.zoho_crm import ZOHO_DATA_CENTERS, ZohoBinaryDownload, ZohoCrmError, ZohoCrmService, ZohoDataCenter

# All requested permissions are read-only. The selected scopes cover the first
# import milestones: organization settings/custom fields, items, customer data,
# quotes, invoices, credit notes, payments, and sales orders.
_ZOHO_BOOKS_SCOPE_VALUES = (
    "ZohoBooks.settings.READ",
    "ZohoBooks.contacts.READ",
    "ZohoBooks.estimates.READ",
    "ZohoBooks.invoices.READ",
    "ZohoBooks.customerpayments.READ",
    "ZohoBooks.creditnotes.READ",
    "ZohoBooks.salesorders.READ",
)
ZOHO_BOOKS_SCOPES = ",".join(_ZOHO_BOOKS_SCOPE_VALUES)


class ZohoBooksError(ValueError):
    pass


@dataclass(frozen=True)
class _CachedAccessToken:
    value: str
    expires_at: datetime


@dataclass(frozen=True)
class ZohoBooksOrganization:
    id: str
    name: str
    is_default: bool


@dataclass(frozen=True)
class ZohoBooksConnectionStatus:
    configured: bool
    connected: bool
    crm_client_available: bool
    data_center: ZohoDataCenter
    redirect_uri: str
    organization_id: str | None
    organization_name: str | None
    available_organizations: tuple[ZohoBooksOrganization, ...]
    connected_at: datetime | None
    last_organization_check_at: datetime | None
    last_error: str | None

    @property
    def requires_organization_selection(self) -> bool:
        return self.connected and self.organization_id is None and bool(self.available_organizations)

    @property
    def ready_for_import(self) -> bool:
        return self.connected and self.organization_id is not None


class ZohoBooksService:
    """Stores a dedicated Books token while reusing the configured OAuth client."""

    _access_token_cache: dict[str, _CachedAccessToken] = {}
    _access_token_cache_lock = threading.RLock()

    def __init__(self, *, db: Session, cipher: SecretCipher, public_base_url: str):
        self.db = db
        self.cipher = cipher
        self.public_base_url = public_base_url.rstrip("/")

    @property
    def redirect_uri(self) -> str:
        # This URI is already registered for the existing Zoho server OAuth app.
        return f"{self.public_base_url}/account/zoho/callback"

    def get_connection(self) -> ZohoBooksConnection | None:
        return self.db.scalar(select(ZohoBooksConnection).order_by(ZohoBooksConnection.id.asc()))

    def get_status(self) -> ZohoBooksConnectionStatus:
        connection = self.get_connection()
        crm_connection = self._get_crm_connection()
        data_center = self._data_center(connection.data_center if connection is not None else (
            crm_connection.data_center if crm_connection is not None else "eu"
        ))
        return ZohoBooksConnectionStatus(
            configured=connection is not None,
            connected=connection is not None and connection.encrypted_refresh_token is not None,
            crm_client_available=crm_connection is not None,
            data_center=data_center,
            redirect_uri=self.redirect_uri,
            organization_id=connection.organization_id if connection is not None else None,
            organization_name=connection.organization_name if connection is not None else None,
            available_organizations=self._stored_organizations(connection),
            connected_at=connection.connected_at if connection is not None else None,
            last_organization_check_at=connection.last_organization_check_at if connection is not None else None,
            last_error=connection.last_error if connection is not None else None,
        )

    def prepare_authorization(self, *, actor: HubUser) -> ZohoBooksConnection:
        self._require_admin(actor)
        crm_connection = self._get_crm_connection()
        if crm_connection is None:
            raise ZohoBooksError("Zoho CRM OAuth-Zugangsdaten fehlen. Richte sie zuerst unter Zoho CRM ein.")
        connection = self.get_connection()
        if connection is None:
            connection = ZohoBooksConnection(
                data_center=crm_connection.data_center,
                encrypted_client_id=crm_connection.encrypted_client_id,
                encrypted_client_secret=crm_connection.encrypted_client_secret,
                scopes=ZOHO_BOOKS_SCOPES,
                configured_by_user_id=actor.id,
            )
            self.db.add(connection)
        else:
            connection.data_center = crm_connection.data_center
            connection.api_domain = None
            connection.encrypted_client_id = crm_connection.encrypted_client_id
            connection.encrypted_client_secret = crm_connection.encrypted_client_secret
            connection.encrypted_refresh_token = None
            connection.scopes = ZOHO_BOOKS_SCOPES
            connection.organization_id = None
            connection.organization_name = None
            connection.available_organizations_json = None
            connection.connected_at = None
            connection.last_organization_check_at = None
            connection.last_error = None
            connection.configured_by_user_id = actor.id
        self.db.flush()
        return connection

    def build_authorization_url(self, *, state: str) -> str:
        connection = self._require_connection()
        if not self._is_valid_state(state):
            raise ZohoBooksError("Die Zoho-Books-Verbindungsanfrage ist abgelaufen. Starte sie erneut.")
        data_center = self._data_center(connection.data_center)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._decrypt(connection.encrypted_client_id, "Zoho client ID"),
                "scope": ZOHO_BOOKS_SCOPES,
                "redirect_uri": self.redirect_uri,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
        return f"{data_center.accounts_domain}/oauth/v2/auth?{query}"

    def complete_authorization(self, *, code: str) -> tuple[ZohoBooksOrganization, ...]:
        connection = self._require_connection()
        normalized_code = code.strip()
        if len(normalized_code) < 8 or len(normalized_code) > 4096 or any(character.isspace() for character in normalized_code):
            raise ZohoBooksError("Zoho hat keinen gültigen Autorisierungscode zurückgegeben.")

        data_center = self._data_center(connection.data_center)
        token_data = self._request_json(
            f"{data_center.accounts_domain}/oauth/v2/token",
            method="POST",
            form={
                "grant_type": "authorization_code",
                "client_id": self._decrypt(connection.encrypted_client_id, "Zoho client ID"),
                "client_secret": self._decrypt(connection.encrypted_client_secret, "Zoho client secret"),
                "redirect_uri": self.redirect_uri,
                "code": normalized_code,
            },
        )
        refresh_token = token_data.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ZohoBooksError("Zoho hat keinen dauerhaften Books-Lesezugriff erteilt. Bestätige die Freigabe erneut.")

        connection.encrypted_refresh_token = self.cipher.encrypt(refresh_token)
        connection.scopes = ZOHO_BOOKS_SCOPES
        connection.api_domain = self._safe_api_domain(token_data.get("api_domain"), data_center)
        connection.connected_at = datetime.now(UTC)
        connection.last_error = None
        organizations = self.refresh_organizations()
        return organizations

    def refresh_organizations(self) -> tuple[ZohoBooksOrganization, ...]:
        connection = self._require_connected_connection()
        response = self._api_get(connection, "/books/v3/organizations")
        organizations = self._organizations_from_response(response)
        if not organizations:
            raise ZohoBooksError("Für diesen Zoho-Books-Zugang wurde keine Organisation gefunden.")

        connection.available_organizations_json = json.dumps(
            [{"id": item.id, "name": item.name, "is_default": item.is_default} for item in organizations],
            ensure_ascii=False,
        )
        known_ids = {item.id for item in organizations}
        if connection.organization_id not in known_ids:
            preferred = next((item for item in organizations if item.is_default), organizations[0] if len(organizations) == 1 else None)
            connection.organization_id = preferred.id if preferred is not None else None
            connection.organization_name = preferred.name if preferred is not None else None
        else:
            selected = next(item for item in organizations if item.id == connection.organization_id)
            connection.organization_name = selected.name
        connection.last_organization_check_at = datetime.now(UTC)
        connection.last_error = None
        self.db.flush()
        return organizations

    def select_organization(self, *, actor: HubUser, organization_id: str) -> ZohoBooksConnection:
        self._require_admin(actor)
        connection = self._require_connected_connection()
        normalized_id = organization_id.strip()
        organization = next((item for item in self._stored_organizations(connection) if item.id == normalized_id), None)
        if organization is None:
            raise ZohoBooksError("Wähle eine der mit diesem Zoho-Books-Zugang verfügbaren Organisationen.")
        connection.organization_id = organization.id
        connection.organization_name = organization.name
        connection.last_error = None
        self.db.flush()
        return connection

    def remove_connection(self, *, actor: HubUser) -> None:
        self._require_admin(actor)
        connection = self.get_connection()
        if connection is None:
            raise ZohoBooksError("Es ist keine Zoho-Books-Verbindung gespeichert.")
        self.db.delete(connection)

    def list_recent_invoice_ids(self, *, limit: int) -> tuple[str, ...]:
        """Return a bounded, newest-first snapshot for a later background import."""
        if limit < 1 or limit > 200:
            raise ZohoBooksError("Der Rechnungsimport muss zwischen 1 und 200 Belegen umfassen.")
        connection = self._require_import_connection()
        return self._list_invoice_ids(connection=connection, per_page=limit, maximum_pages=1)

    def list_all_invoice_ids(self) -> tuple[str, ...]:
        """Return every accessible Books invoice ID, newest first, for a resumable bulk import."""
        connection = self._require_import_connection()
        return self._list_invoice_ids(connection=connection, per_page=200, maximum_pages=10_000)

    def _list_invoice_ids(self, *, connection, per_page: int, maximum_pages: int) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for page in range(1, maximum_pages + 1):
            query = urlencode(
                {
                    "organization_id": connection.organization_id or "",
                    "page": str(page),
                    "per_page": str(per_page),
                    "sort_column": "date",
                    "sort_order": "D",
                }
            )
            response = self._api_get(connection, f"/books/v3/invoices?{query}")
            rows = response.get("invoices")
            if not isinstance(rows, list):
                raise ZohoBooksError("Zoho Books hat keine gültige Rechnungsliste zurückgegeben.")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                invoice_id = str(row.get("invoice_id") or "").strip()
                if invoice_id and invoice_id not in seen:
                    seen.add(invoice_id)
                    result.append(invoice_id)

            page_context = response.get("page_context")
            has_more = (
                str(page_context.get("has_more_page", "")).strip().casefold() in {"true", "1", "yes"}
                if isinstance(page_context, dict)
                else len(rows) >= per_page
            )
            if not has_more:
                return tuple(result)
        raise ZohoBooksError("Zoho Books liefert ungewöhnlich viele Rechnungsseiten. Der Import wurde nicht gestartet.")

    def get_invoice(self, *, invoice_id: str) -> dict[str, object]:
        connection = self._require_import_connection()
        normalized_id = self._numeric_identifier(invoice_id, "Rechnung")
        query = urlencode({"organization_id": connection.organization_id or ""})
        response = self._api_get(connection, f"/books/v3/invoices/{normalized_id}?{query}")
        invoice = response.get("invoice")
        if not isinstance(invoice, dict):
            raise ZohoBooksError("Zoho Books hat keine gültigen Rechnungsdaten zurückgegeben.")
        return invoice

    def list_all_recurring_invoice_ids(self) -> tuple[str, ...]:
        """Return every accessible recurring invoice ID for a resumable full import."""
        connection = self._require_import_connection()
        result: list[str] = []
        seen: set[str] = set()
        per_page = 200
        for page in range(1, 10_001):
            query = urlencode(
                {
                    "organization_id": connection.organization_id or "",
                    "page": str(page),
                    "per_page": str(per_page),
                    "sort_column": "created_time",
                    "sort_order": "D",
                }
            )
            response = self._api_get(connection, f"/books/v3/recurringinvoices?{query}")
            rows = response.get("recurring_invoices")
            if not isinstance(rows, list):
                raise ZohoBooksError("Zoho Books hat keine gültige Liste periodischer Rechnungen zurückgegeben.")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                recurring_invoice_id = str(row.get("recurring_invoice_id") or "").strip()
                if recurring_invoice_id and recurring_invoice_id not in seen:
                    seen.add(recurring_invoice_id)
                    result.append(recurring_invoice_id)

            page_context = response.get("page_context")
            has_more = (
                str(page_context.get("has_more_page", "")).strip().casefold() in {"true", "1", "yes"}
                if isinstance(page_context, dict)
                else len(rows) >= per_page
            )
            if not has_more:
                return tuple(result)
        raise ZohoBooksError(
            "Zoho Books liefert ungewöhnlich viele Seiten periodischer Rechnungen. Der Import wurde nicht gestartet."
        )

    def get_recurring_invoice(self, *, recurring_invoice_id: str) -> dict[str, object]:
        connection = self._require_import_connection()
        normalized_id = self._numeric_identifier(recurring_invoice_id, "Periodische Rechnung")
        query = urlencode({"organization_id": connection.organization_id or ""})
        response = self._api_get(
            connection,
            f"/books/v3/recurringinvoices/{normalized_id}?{query}",
        )
        recurring_invoice = response.get("recurring_invoice")
        if not isinstance(recurring_invoice, dict):
            raise ZohoBooksError("Zoho Books hat keine gültigen Daten der periodischen Rechnung zurückgegeben.")
        return recurring_invoice

    def get_books_contact(self, *, contact_id: str) -> dict[str, object]:
        """Resolve a Books customer to its linked CRM account and contact IDs."""
        connection = self._require_import_connection()
        normalized_id = self._numeric_identifier(contact_id, "Zoho-Books-Kunde")
        query = urlencode({"organization_id": connection.organization_id or ""})
        response = self._api_get(connection, f"/books/v3/contacts/{normalized_id}?{query}")
        contact = response.get("contact")
        if not isinstance(contact, dict):
            raise ZohoBooksError("Zoho Books hat keine gültigen Kundendaten zurückgegeben.")
        return contact

    def download_invoice_pdf(self, *, invoice_id: str) -> ZohoBinaryDownload:
        """Fetch the PDF Books currently provides, including a ZUGFeRD PDF when configured there."""
        connection = self._require_import_connection()
        normalized_id = self._numeric_identifier(invoice_id, "Rechnung")
        query = urlencode({"organization_id": connection.organization_id or "", "accept": "pdf"})
        download = self._api_get_binary(connection, f"/books/v3/invoices/{normalized_id}?{query}")
        if download.content_type != "application/pdf" or not download.content.startswith(b"%PDF-"):
            raise ZohoBooksError("Zoho Books hat für diese Rechnung keine PDF-Datei zurückgegeben.")
        return download

    def record_error(self, message: str) -> None:
        connection = self.get_connection()
        if connection is not None:
            connection.last_error = message[:255]

    @staticmethod
    def new_oauth_state() -> str:
        return secrets.token_urlsafe(32)

    def _api_get(self, connection: ZohoBooksConnection, path: str) -> dict[str, object]:
        access_token = self._refresh_access_token(connection)
        data_center = self._data_center(connection.data_center)
        api_domain = connection.api_domain or data_center.api_domain
        return self._request_json(
            f"{api_domain}{path}",
            method="GET",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )

    def _api_get_binary(self, connection: ZohoBooksConnection, path: str) -> ZohoBinaryDownload:
        access_token = self._refresh_access_token(connection)
        data_center = self._data_center(connection.data_center)
        api_domain = connection.api_domain or data_center.api_domain
        try:
            return ZohoCrmService._request_binary(
                f"{api_domain}{path}",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}", "Accept": "application/pdf"},
            )
        except ZohoCrmError as exc:
            raise ZohoBooksError(str(exc).replace("Zoho CRM", "Zoho Books")) from exc

    def _refresh_access_token(self, connection: ZohoBooksConnection) -> str:
        cache_key = self._access_token_cache_key(connection)
        now = datetime.now(UTC)
        with self._access_token_cache_lock:
            cached = self._access_token_cache.get(cache_key)
            if cached is not None and cached.expires_at > now:
                return cached.value

        refresh_token = self._decrypt(connection.encrypted_refresh_token or "", "Zoho Books refresh token")
        data_center = self._data_center(connection.data_center)
        response = self._request_json(
            f"{data_center.accounts_domain}/oauth/v2/token",
            method="POST",
            form={
                "grant_type": "refresh_token",
                "client_id": self._decrypt(connection.encrypted_client_id, "Zoho client ID"),
                "client_secret": self._decrypt(connection.encrypted_client_secret, "Zoho client secret"),
                "refresh_token": refresh_token,
            },
        )
        access_token = response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ZohoBooksError("Zoho konnte den Books-Zugriff nicht erneuern. Verbinde Zoho Books erneut.")
        connection.api_domain = self._safe_api_domain(response.get("api_domain"), data_center)
        expires_in = response.get("expires_in")
        lifetime_seconds = expires_in if isinstance(expires_in, int) and expires_in > 0 else 3600
        # Reuse the valid one-hour token and refresh it one minute before expiry.
        expires_at = now + timedelta(seconds=max(lifetime_seconds - 60, 60))
        with self._access_token_cache_lock:
            self._access_token_cache[cache_key] = _CachedAccessToken(access_token, expires_at)
        return access_token

    @staticmethod
    def _access_token_cache_key(connection: ZohoBooksConnection) -> str:
        refresh_token = connection.encrypted_refresh_token or ""
        token_digest = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        return f"{connection.id or 'new'}:{token_digest}"

    def _require_connection(self) -> ZohoBooksConnection:
        connection = self.get_connection()
        if connection is None:
            raise ZohoBooksError("Verbinde zuerst Zoho Books.")
        return connection

    def _require_connected_connection(self) -> ZohoBooksConnection:
        connection = self._require_connection()
        if connection.encrypted_refresh_token is None:
            raise ZohoBooksError("Verbinde Zoho Books, bevor Organisationen gelesen werden können.")
        return connection

    def _require_import_connection(self) -> ZohoBooksConnection:
        connection = self._require_connected_connection()
        if not connection.organization_id:
            raise ZohoBooksError("Wähle zuerst die Zoho-Books-Organisation für den Import aus.")
        return connection

    def _get_crm_connection(self) -> ZohoConnection | None:
        return self.db.scalar(select(ZohoConnection).order_by(ZohoConnection.id.asc()))

    @staticmethod
    def _organizations_from_response(response: dict[str, object]) -> tuple[ZohoBooksOrganization, ...]:
        rows = response.get("organizations")
        if not isinstance(rows, list):
            raise ZohoBooksError("Zoho Books hat keine gültige Organisationsliste zurückgegeben.")
        organizations: list[ZohoBooksOrganization] = []
        seen_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get("organization_id") or "").strip()
            name = str(row.get("name") or "").strip()
            if not identifier or not name or identifier in seen_ids:
                continue
            seen_ids.add(identifier)
            organizations.append(
                ZohoBooksOrganization(id=identifier, name=name[:255], is_default=ZohoBooksService._as_bool(row.get("is_default_org")))
            )
        return tuple(organizations)

    @staticmethod
    def _stored_organizations(connection: ZohoBooksConnection | None) -> tuple[ZohoBooksOrganization, ...]:
        if connection is None or not connection.available_organizations_json:
            return ()
        try:
            rows = json.loads(connection.available_organizations_json)
        except (TypeError, json.JSONDecodeError):
            return ()
        if not isinstance(rows, list):
            return ()
        organizations: list[ZohoBooksOrganization] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get("id") or "").strip()
            name = str(row.get("name") or "").strip()
            if identifier and name:
                organizations.append(ZohoBooksOrganization(identifier, name[:255], ZohoBooksService._as_bool(row.get("is_default"))))
        return tuple(organizations)

    @staticmethod
    def _require_admin(actor: HubUser) -> None:
        if actor.role != "admin":
            raise ZohoBooksError("Nur Hub-Administratoren können Zoho Books verbinden.")

    @staticmethod
    def _is_valid_state(value: str) -> bool:
        return len(value) >= 32 and len(value) <= 256 and all(character.isalnum() or character in "-_" for character in value)

    @staticmethod
    def _data_center(value: str) -> ZohoDataCenter:
        data_center = ZOHO_DATA_CENTERS.get(value)
        if data_center is None:
            raise ZohoBooksError("Wähle ein unterstütztes Zoho-Rechenzentrum.")
        return data_center

    @staticmethod
    def _safe_api_domain(value: object, data_center: ZohoDataCenter) -> str:
        if not isinstance(value, str):
            return data_center.api_domain
        parsed = urlsplit(value)
        if parsed.scheme == "https" and value.rstrip("/") == data_center.api_domain:
            return data_center.api_domain
        return data_center.api_domain

    @staticmethod
    def _numeric_identifier(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized.isdigit() or len(normalized) > 255:
            raise ZohoBooksError(f"Die Zoho-Books-ID für {label} ist ungültig.")
        return normalized

    @staticmethod
    def _as_bool(value: object) -> bool:
        return value is True or (isinstance(value, str) and value.casefold() == "true")

    @staticmethod
    def _request_json(*args, **kwargs) -> dict[str, object]:
        try:
            return ZohoCrmService._request_json(*args, **kwargs)
        except ZohoCrmError as exc:
            raise ZohoBooksError(str(exc).replace("Zoho CRM", "Zoho Books")) from exc

    def _decrypt(self, encrypted_value: str, label: str) -> str:
        try:
            return self.cipher.decrypt(encrypted_value)
        except Exception as exc:
            raise ZohoBooksError(f"Der gespeicherte Wert f\u00fcr {label} kann nicht entschl\u00fcsselt werden. Verbinde Zoho Books erneut.") from exc
