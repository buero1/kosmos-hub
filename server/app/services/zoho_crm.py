"""Zoho CRM connection and the current status-filtered Account synchronization."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.models.site import Site
from app.models.zoho_connection import ZohoConnection

ZOHO_ACCOUNT_MODULE = "Accounts"
# Request the complete CRM API scope once. Individual Hub workflows still decide
# whether a connected capability may create, change, or delete CRM data.
ZOHO_CRM_SCOPES = ",".join(
    (
        "ZohoCRM.modules.ALL",
        "ZohoCRM.settings.ALL",
        "ZohoCRM.users.ALL",
        "ZohoCRM.org.ALL",
        "ZohoCRM.bulk.ALL",
        "ZohoCRM.coql.READ",
        "ZohoCRM.notifications.READ",
        "ZohoCRM.notifications.CREATE",
        "ZohoCRM.notifications.UPDATE",
        "ZohoCRM.notifications.DELETE",
        "ZohoCRM.apis.READ",
        "ZohoCRM.send_mail.all.CREATE",
        "ZohoCRM.share.all",
        "ZohoCRM.signals.ALL",
        "ZohoSearch.securesearch.READ",
    )
)
_REQUEST_TIMEOUT_SECONDS = 20
_MAX_PAGE_REQUESTS = 10
ZOHO_RELEVANT_ACCOUNT_STATUSES = ("Aktuell", "Neu", "gekündigt", "Kündigung liegt vor")


@dataclass(frozen=True)
class ZohoDataCenter:
    key: str
    label: str
    accounts_domain: str
    api_domain: str


ZOHO_DATA_CENTERS = {
    "eu": ZohoDataCenter("eu", "Europe (zoho.eu)", "https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "global": ZohoDataCenter("global", "Global (zoho.com)", "https://accounts.zoho.com", "https://www.zohoapis.com"),
    "in": ZohoDataCenter("in", "India (zoho.in)", "https://accounts.zoho.in", "https://www.zohoapis.in"),
    "au": ZohoDataCenter("au", "Australia (zoho.com.au)", "https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "jp": ZohoDataCenter("jp", "Japan (zoho.jp)", "https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "ca": ZohoDataCenter("ca", "Canada (zoho.ca)", "https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
}


@dataclass(frozen=True)
class ZohoAccountField:
    key: str
    label: str
    required_for_identity: bool = False


ZOHO_ACCOUNT_FIELDS = (
    ZohoAccountField("record_id", "Eintrag-ID", True),
    ZohoAccountField("customer_name", "Kunde-Name", True),
    ZohoAccountField("account_status", "Status", True),
    ZohoAccountField("phone", "Tel."),
    ZohoAccountField("website", "Webseite"),
    ZohoAccountField("customer_number", "Kunde-Nummer"),
    ZohoAccountField("industry", "Branche"),
    ZohoAccountField("billing_street", "Rechnungsadresse - Straße"),
    ZohoAccountField("billing_city", "Rechnungsadresse - Stadt"),
    ZohoAccountField("billing_postal_code", "Rechnungsadresse - PLZ"),
    ZohoAccountField("billing_country", "Rechnungsadresse - Land"),
    ZohoAccountField("work_domain_login", "Arbeitsdomain-Login"),
    ZohoAccountField("compact_phone", "Tel_komprimiert"),
    ZohoAccountField("previous_website", "Bisherige (alte) Website"),
    ZohoAccountField("dialfire_id", "Dialfire-ID"),
    ZohoAccountField("important_info", "Wichtige Infos"),
    ZohoAccountField("contact_email", "Kontakt-E-Mail"),
    ZohoAccountField("contact_salutation", "Kontakt-Briefanrede"),
    ZohoAccountField("contact_last_name", "Kontakt-Nachname"),
    ZohoAccountField("duration_minutes", "Dauer in Minuten"),
    ZohoAccountField("update_note", "Update-Notiz"),
    ZohoAccountField("update_date", "Update-Datum"),
    ZohoAccountField("last_update_date", "Letztes Update-Datum"),
    ZohoAccountField("contact_first_name", "Kontakt-Vorname"),
)


class ZohoCrmError(ValueError):
    pass


@dataclass(frozen=True)
class ZohoConnectionStatus:
    configured: bool
    connected: bool
    data_center: ZohoDataCenter
    redirect_uri: str
    connected_at: datetime | None
    last_metadata_at: datetime | None
    last_sync_at: datetime | None
    last_error: str | None
    mapped_field_count: int
    missing_fields: tuple[str, ...]
    requires_scope_reconnect: bool


@dataclass(frozen=True)
class ZohoFieldMappingRow:
    key: str
    label: str
    api_name: str | None
    required_for_identity: bool


@dataclass(frozen=True)
class ZohoSyncResult:
    created_customers: int
    updated_customers: int
    relevant_accounts: int
    unique_site_match_candidates: int
    unmapped_fields: tuple[str, ...]


class ZohoCrmService:
    """Keeps OAuth credentials encrypted and synchronizes Accounts safely."""

    def __init__(self, *, db: Session, cipher: SecretCipher, public_base_url: str):
        self.db = db
        self.cipher = cipher
        self.public_base_url = public_base_url.rstrip("/")

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/account/zoho/callback"

    def get_connection(self) -> ZohoConnection | None:
        return self.db.scalar(select(ZohoConnection).order_by(ZohoConnection.id.asc()))

    def get_status(self) -> ZohoConnectionStatus:
        connection = self.get_connection()
        data_center = self._data_center(connection.data_center if connection is not None else "eu")
        field_map = self._stored_field_map(connection)
        rows = self.mapping_rows(field_map)
        return ZohoConnectionStatus(
            configured=connection is not None,
            connected=connection is not None and connection.encrypted_refresh_token is not None,
            data_center=data_center,
            redirect_uri=self.redirect_uri,
            connected_at=connection.connected_at if connection is not None else None,
            last_metadata_at=connection.last_metadata_at if connection is not None else None,
            last_sync_at=connection.last_sync_at if connection is not None else None,
            last_error=connection.last_error if connection is not None else None,
            mapped_field_count=sum(row.api_name is not None for row in rows),
            missing_fields=tuple(row.label for row in rows if row.api_name is None),
            requires_scope_reconnect=connection is not None and connection.encrypted_refresh_token is not None and not self._has_current_scope_grant(connection),
        )

    def configure(self, *, actor: HubUser, data_center: str, client_id: str, client_secret: str) -> ZohoConnection:
        self._require_admin(actor)
        center = self._data_center(data_center)
        normalized_client_id = self._normalize_secret(client_id, "Zoho client ID")
        normalized_client_secret = self._normalize_secret(client_secret, "Zoho client secret")
        connection = self.get_connection()
        if connection is None:
            connection = ZohoConnection(
                data_center=center.key,
                encrypted_client_id=self.cipher.encrypt(normalized_client_id),
                encrypted_client_secret=self.cipher.encrypt(normalized_client_secret),
                scopes=ZOHO_CRM_SCOPES,
                configured_by_user_id=actor.id,
            )
            self.db.add(connection)
        else:
            connection.data_center = center.key
            connection.api_domain = None
            connection.encrypted_client_id = self.cipher.encrypt(normalized_client_id)
            connection.encrypted_client_secret = self.cipher.encrypt(normalized_client_secret)
            connection.encrypted_refresh_token = None
            connection.scopes = ZOHO_CRM_SCOPES
            connection.field_map_json = None
            connection.connected_at = None
            connection.last_metadata_at = None
            connection.last_sync_at = None
            connection.last_error = None
            connection.configured_by_user_id = actor.id
        self.db.flush()
        return connection

    def build_authorization_url(self, *, state: str) -> str:
        connection = self._require_connection()
        if not self._is_valid_state(state):
            raise ZohoCrmError("The Zoho connection request expired. Start the connection again.")
        client_id = self._decrypt(connection.encrypted_client_id, "Zoho client ID")
        data_center = self._data_center(connection.data_center)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "scope": ZOHO_CRM_SCOPES,
                "redirect_uri": self.redirect_uri,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
        return f"{data_center.accounts_domain}/oauth/v2/auth?{query}"

    def complete_authorization(self, *, code: str) -> ZohoConnection:
        connection = self._require_connection()
        normalized_code = code.strip()
        if len(normalized_code) < 8 or len(normalized_code) > 4096 or any(character.isspace() for character in normalized_code):
            raise ZohoCrmError("Zoho did not return a valid authorization code.")

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
            raise ZohoCrmError("Zoho did not grant persistent read access. Please approve the requested access again.")

        connection.encrypted_refresh_token = self.cipher.encrypt(refresh_token)
        connection.scopes = ZOHO_CRM_SCOPES
        connection.api_domain = self._safe_api_domain(token_data.get("api_domain"), data_center)
        connection.connected_at = datetime.now(UTC)
        connection.last_error = None
        self.db.flush()
        return connection

    def refresh_field_mapping(self) -> list[ZohoFieldMappingRow]:
        connection = self._require_connected_connection()
        response = self._api_get(connection, "/crm/v8/settings/fields", {"module": ZOHO_ACCOUNT_MODULE})
        fields = response.get("fields")
        if not isinstance(fields, list):
            raise ZohoCrmError("Zoho returned no field metadata for Accounts.")

        field_map = self.resolve_account_field_mapping(fields)
        connection.field_map_json = json.dumps(field_map, ensure_ascii=True, sort_keys=True)
        connection.last_metadata_at = datetime.now(UTC)
        connection.last_error = None
        self.db.flush()
        return self.mapping_rows(field_map)

    def sync_accounts(self) -> ZohoSyncResult:
        connection = self._require_connected_connection()
        if not self._has_current_scope_grant(connection):
            raise ZohoCrmError("Reconnect Zoho CRM once to approve the full CRM scope before status-filtered Account synchronization.")
        mapping_rows = self.refresh_field_mapping()
        mapping = {row.key: row.api_name for row in mapping_rows}
        required_fields = ("customer_name", "account_status")
        missing_required = [next(field.label for field in ZOHO_ACCOUNT_FIELDS if field.key == key) for key in required_fields if mapping[key] is None]
        if missing_required:
            raise ZohoCrmError(f"The required Zoho Account field(s) {', '.join(missing_required)} were not found. No customers were changed.")

        records = self._get_relevant_account_records(connection, mapping)
        created_customers = 0
        updated_customers = 0
        unique_site_match_candidates = 0
        synced_at = datetime.now(UTC)

        for record in records:
            record_id = self._as_text(record.get("id"))
            if not record_id:
                continue

            profile = self._build_profile(record, mapping, synced_at)
            name = self._as_text(record.get(mapping["customer_name"])) or f"Zoho Account {record_id}"
            customer_number = self._as_text(record.get(mapping["customer_number"]))
            website_domain = self.normalize_website_domain(record.get(mapping["website"]))
            customer = self.db.scalar(select(Customer).where(Customer.zoho_id == record_id))
            if customer is None:
                customer = Customer(zoho_id=record_id, name=name)
                self.db.add(customer)
                created_customers += 1
            else:
                updated_customers += 1

            customer.name = name
            customer.external_id = customer_number
            customer.website_domain = website_domain
            customer.encrypted_profile_json = self.cipher.encrypt(json.dumps(profile, ensure_ascii=False, default=str))
            customer.zoho_modified_at = self._parse_datetime(record.get("Modified_Time"))
            customer.zoho_synced_at = synced_at
            self.db.flush()

            if website_domain and self._has_unique_unlinked_site_match(website_domain):
                unique_site_match_candidates += 1

        connection.last_sync_at = synced_at
        connection.last_error = None
        self.db.flush()
        return ZohoSyncResult(
            created_customers=created_customers,
            updated_customers=updated_customers,
            relevant_accounts=len(records),
            unique_site_match_candidates=unique_site_match_candidates,
            unmapped_fields=tuple(row.label for row in mapping_rows if row.api_name is None),
        )

    def remove_connection(self, *, actor: HubUser) -> None:
        self._require_admin(actor)
        connection = self.get_connection()
        if connection is None:
            raise ZohoCrmError("No Zoho CRM connection is stored in the Hub.")
        self.db.delete(connection)

    def record_error(self, message: str) -> None:
        connection = self.get_connection()
        if connection is not None:
            connection.last_error = message[:255]

    def mapping_rows(self, field_map: dict[str, Any] | None = None) -> list[ZohoFieldMappingRow]:
        source = field_map or self._stored_field_map(self.get_connection())
        values = source.get("fields", {}) if isinstance(source, dict) else {}
        return [
            ZohoFieldMappingRow(
                key=field.key,
                label=field.label,
                api_name="id" if field.key == "record_id" else self._as_text(values.get(field.key)),
                required_for_identity=field.required_for_identity,
            )
            for field in ZOHO_ACCOUNT_FIELDS
        ]

    @staticmethod
    def resolve_account_field_mapping(fields: list[object]) -> dict[str, object]:
        available: dict[str, set[str]] = {}
        for field in fields:
            if not isinstance(field, dict):
                continue
            api_name = field.get("api_name")
            if not isinstance(api_name, str) or not api_name.strip():
                continue
            for candidate in (field.get("field_label"), field.get("display_label"), api_name):
                normalized = ZohoCrmService._normalize_field_label(candidate)
                if normalized:
                    available.setdefault(normalized, set()).add(api_name)

        mapped: dict[str, str | None] = {}
        for field in ZOHO_ACCOUNT_FIELDS:
            if field.key == "record_id":
                mapped[field.key] = "id"
                continue
            matches = available.get(ZohoCrmService._normalize_field_label(field.label), set())
            mapped[field.key] = next(iter(matches)) if len(matches) == 1 else None
        return {"module": ZOHO_ACCOUNT_MODULE, "fields": mapped}

    @staticmethod
    def normalize_website_domain(value: object) -> str | None:
        text = ZohoCrmService._as_text(value)
        if not text:
            return None
        parsed = urlsplit(text if "://" in text else f"https://{text}")
        hostname = parsed.hostname.lower().strip(".") if parsed.hostname else ""
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname or None

    def _get_relevant_account_records(self, connection: ZohoConnection, mapping: dict[str, str | None]) -> list[dict[str, object]]:
        status_field = mapping["account_status"]
        assert status_field is not None
        requested_fields = sorted({api_name for api_name in mapping.values() if api_name and api_name != "id"} | {"Modified_Time"})
        records_by_id: dict[str, dict[str, object]] = {}
        for account_status in ZOHO_RELEVANT_ACCOUNT_STATUSES:
            for page in range(1, _MAX_PAGE_REQUESTS + 1):
                response = self._api_get(
                    connection,
                    f"/crm/v8/{ZOHO_ACCOUNT_MODULE}/search",
                    {
                        "criteria": f"({status_field}:equals:{account_status})",
                        "fields": ",".join(requested_fields),
                        "per_page": "200",
                        "page": str(page),
                    },
                    allow_empty_response=True,
                )
                data = response.get("data")
                if not isinstance(data, list):
                    raise ZohoCrmError("Zoho returned an invalid filtered Accounts response. No customers were changed.")
                for record in data:
                    if not isinstance(record, dict):
                        continue
                    record_id = self._as_text(record.get("id"))
                    if record_id and self._is_relevant_account_status(record.get(status_field)):
                        records_by_id[record_id] = record
                info = response.get("info")
                more_records = isinstance(info, dict) and info.get("more_records") is True
                if not more_records:
                    break
            else:
                raise ZohoCrmError(f"Zoho returned more than 2,000 Accounts with status “{account_status}”. Bulk synchronization must be enabled before importing them.")
        return list(records_by_id.values())

    def _api_get(
        self,
        connection: ZohoConnection,
        path: str,
        params: dict[str, str],
        *,
        allow_empty_response: bool = False,
    ) -> dict[str, object]:
        access_token = self._refresh_access_token(connection)
        data_center = self._data_center(connection.data_center)
        api_domain = connection.api_domain or data_center.api_domain
        url = f"{api_domain}{path}?{urlencode(params)}"
        return self._request_json(
            url,
            method="GET",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            allow_empty_response=allow_empty_response,
        )

    def _refresh_access_token(self, connection: ZohoConnection) -> str:
        refresh_token = self._decrypt(connection.encrypted_refresh_token or "", "Zoho refresh token")
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
            raise ZohoCrmError("Zoho could not refresh the access token. Reconnect the Zoho account.")
        connection.api_domain = self._safe_api_domain(response.get("api_domain"), data_center)
        return access_token

    def _build_profile(self, record: dict[str, object], mapping: dict[str, str | None], synced_at: datetime) -> dict[str, object]:
        profile: dict[str, object] = {
            "source": "zoho-crm-accounts",
            "record_id": self._as_text(record.get("id")),
            "modified_time": self._as_text(record.get("Modified_Time")),
            "synced_at": synced_at.isoformat(),
            "fields": {},
        }
        values = profile["fields"]
        assert isinstance(values, dict)
        for field in ZOHO_ACCOUNT_FIELDS:
            api_name = mapping.get(field.key)
            values[field.label] = record.get(api_name) if api_name else None
        return profile

    def _has_unique_unlinked_site_match(self, website_domain: str) -> bool:
        candidates = [
            site
            for site in self.db.scalars(select(Site).where(Site.customer_id.is_(None))).all()
            if self.normalize_website_domain(site.domain) == website_domain
        ]
        return len(candidates) == 1

    @staticmethod
    def _is_relevant_account_status(value: object) -> bool:
        status = ZohoCrmService._as_text(value)
        return status is not None and status.casefold() in {item.casefold() for item in ZOHO_RELEVANT_ACCOUNT_STATUSES}

    @staticmethod
    def _has_current_scope_grant(connection: ZohoConnection) -> bool:
        granted_scopes = {scope.strip() for scope in connection.scopes.split(",")}
        return set(ZOHO_CRM_SCOPES.split(",")).issubset(granted_scopes)

    def _require_connection(self) -> ZohoConnection:
        connection = self.get_connection()
        if connection is None:
            raise ZohoCrmError("Save the Zoho client ID and client secret before connecting Zoho CRM.")
        return connection

    def _require_connected_connection(self) -> ZohoConnection:
        connection = self._require_connection()
        if connection.encrypted_refresh_token is None:
            raise ZohoCrmError("Connect Zoho CRM before reading Accounts.")
        return connection

    @staticmethod
    def _require_admin(actor: HubUser) -> None:
        if actor.role != "admin":
            raise ZohoCrmError("Only Hub administrators can configure Zoho CRM.")

    @staticmethod
    def _normalize_secret(value: str, label: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8 or len(normalized) > 1024 or any(character.isspace() for character in normalized):
            raise ZohoCrmError(f"Enter a valid {label} without spaces.")
        return normalized

    @staticmethod
    def _normalize_field_label(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    @staticmethod
    def _as_text(value: object) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _is_valid_state(value: str) -> bool:
        return len(value) >= 32 and len(value) <= 256 and all(character.isalnum() or character in "-_" for character in value)

    @staticmethod
    def new_oauth_state() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _data_center(value: str) -> ZohoDataCenter:
        data_center = ZOHO_DATA_CENTERS.get(value)
        if data_center is None:
            raise ZohoCrmError("Choose a supported Zoho data center.")
        return data_center

    @staticmethod
    def _safe_api_domain(value: object, data_center: ZohoDataCenter) -> str:
        if not isinstance(value, str):
            return data_center.api_domain
        parsed = urlsplit(value)
        if parsed.scheme == "https" and value.rstrip("/") == data_center.api_domain:
            return data_center.api_domain
        return data_center.api_domain

    def _stored_field_map(self, connection: ZohoConnection | None) -> dict[str, Any]:
        if connection is None or not connection.field_map_json:
            return {}
        try:
            parsed = json.loads(connection.field_map_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _decrypt(self, encrypted_value: str, label: str) -> str:
        try:
            return self.cipher.decrypt(encrypted_value)
        except Exception as exc:
            raise ZohoCrmError(f"The stored {label} could not be decrypted. Save the Zoho connection again.") from exc

    @staticmethod
    def _request_json(
        url: str,
        *,
        method: str,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        allow_empty_response: bool = False,
    ) -> dict[str, object]:
        body = urlencode(form).encode("utf-8") if form is not None else None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - Zoho URLs are fixed above.
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            code = "unknown_error"
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                if isinstance(error_payload, dict):
                    provider_code = error_payload.get("code") or error_payload.get("error")
                    if isinstance(provider_code, str):
                        code = provider_code
            except Exception:
                pass
            raise ZohoCrmError(f"Zoho rejected the request ({code}).") from exc
        except URLError as exc:
            raise ZohoCrmError("Zoho CRM is currently unreachable. Try again shortly.") from exc

        if not payload.strip() and allow_empty_response:
            return {"data": [], "info": {"more_records": False}}
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ZohoCrmError("Zoho returned an invalid response.") from exc
        if not isinstance(decoded, dict):
            raise ZohoCrmError("Zoho returned an invalid response.")
        return decoded
