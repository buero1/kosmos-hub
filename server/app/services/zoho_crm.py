"""Zoho CRM connection and full Account synchronization with status-based visibility."""

from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_user import HubUser
from app.models.site import Site, SiteStatus
from app.models.zoho_connection import ZohoConnection
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS, ZOHO_ACCOUNT_SUBFORMS, ZohoAccountField
from app.services.zoho_contact_field_catalog import ZOHO_CONTACT_FIELDS

ZOHO_ACCOUNT_MODULE = "Accounts"
ZOHO_CONTACT_MODULE = "Contacts"
ZOHO_CASE_MODULE = "Cases"
# Request the complete CRM API scope once. Individual Hub workflows still decide
# whether a connected capability may create, change, or delete CRM data.
_ZOHO_CRM_SCOPE_VALUES = (
        "ZohoCRM.modules.ALL",
        "ZohoCRM.modules.emails.READ",
        "ZohoCRM.modules.notes.ALL",
        "ZohoCRM.settings.ALL",
        "ZohoCRM.settings.emails.READ",
        "ZohoCRM.users.ALL",
        "ZohoCRM.org.ALL",
        "ZohoCRM.bulk.ALL",
        "ZohoCRM.coql.READ",
        "ZohoCRM.apis.READ",
        "ZohoCRM.send_mail.all.CREATE",
        "ZohoCRM.Files.CREATE",
        "ZohoCRM.templates.email.READ",
        "ZohoCRM.share.all",
        "ZohoCRM.signals.ALL",
        "ZohoSearch.securesearch.READ",
)
ZOHO_CRM_SCOPES = ",".join(_ZOHO_CRM_SCOPE_VALUES)
_ZOHO_COMMUNICATION_SCOPE_VALUES = frozenset(
    scope
    for scope in _ZOHO_CRM_SCOPE_VALUES
    if scope not in {"ZohoCRM.templates.email.READ", "ZohoCRM.Files.CREATE"}
)
_ZOHO_EMAIL_TEMPLATE_SCOPE = "ZohoCRM.templates.email.READ"
_ZOHO_FILE_SCOPE = "ZohoCRM.Files.CREATE"
_REQUEST_TIMEOUT_SECONDS = 20
_BINARY_DOWNLOAD_TIMEOUT_SECONDS = 90
_MAX_PAGE_REQUESTS = 10
_MAX_FIELDS_PER_ZOHO_REQUEST = 50
_MAX_CASE_PAGE_REQUESTS = 100
_MAX_EMAIL_ATTACHMENT_BYTES = 25 * 1024 * 1024
_MAX_CONTACT_FIELD_LENGTH = 1_000
_ACCESS_TOKEN_EXPIRY_BUFFER_SECONDS = 90
_DEFAULT_ACCESS_TOKEN_LIFETIME_SECONDS = 3_600
ZOHO_RELEVANT_ACCOUNT_STATUSES = ("Aktuell", "Neu", "gekündigt", "Kündigung liegt vor")
_ACCESS_TOKEN_CACHE: dict[str, tuple[str, datetime]] = {}
_ACCESS_TOKEN_CACHE_LOCK = Lock()


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
    synchronized_accounts: int
    visible_accounts: int
    hidden_customers: int
    created_sites: int
    linked_sites: int
    site_conflicts: int
    synchronized_contacts: int
    created_contacts: int
    updated_contacts: int
    removed_contacts: int
    unmapped_fields: tuple[str, ...]


@dataclass(frozen=True)
class ZohoContactSyncResult:
    synchronized_contacts: int
    created_contacts: int
    updated_contacts: int
    removed_contacts: int


@dataclass(frozen=True)
class ZohoBinaryDownload:
    content: bytes
    content_type: str


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
        self._clear_cached_access_token(connection)
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
        self._clear_cached_access_token(connection)
        return connection

    def refresh_field_mapping(self) -> list[ZohoFieldMappingRow]:
        connection = self._require_connected_connection()
        response = self._api_get(connection, "/crm/v8/settings/fields", {"module": ZOHO_ACCOUNT_MODULE})
        fields = response.get("fields")
        if not isinstance(fields, list):
            raise ZohoCrmError("Zoho returned no field metadata for Accounts.")

        field_map = self.resolve_account_field_mapping(fields)
        field_map["metadata"] = self._field_metadata_by_key(ZOHO_ACCOUNT_FIELDS, fields, field_map["fields"])
        field_map["subforms"] = self._subform_field_maps(connection, fields)
        connection.field_map_json = json.dumps(field_map, ensure_ascii=True, sort_keys=True)
        connection.last_metadata_at = datetime.now(UTC)
        connection.last_error = None
        self.db.flush()
        return self.mapping_rows(field_map)

    def sync_accounts(self) -> ZohoSyncResult:
        connection = self._require_connected_connection()
        if not self._has_current_scope_grant(connection):
            raise ZohoCrmError("Reconnect Zoho CRM once to approve the full CRM scope before full Account synchronization.")
        mapping_rows = self.refresh_field_mapping()
        field_map = self._stored_field_map(connection)
        mapping = {row.key: row.api_name for row in mapping_rows}
        required_fields = ("customer_name", "account_status")
        missing_required = [next(field.label for field in ZOHO_ACCOUNT_FIELDS if field.key == key) for key in required_fields if mapping[key] is None]
        if missing_required:
            raise ZohoCrmError(f"The required Zoho Account field(s) {', '.join(missing_required)} were not found. No customers were changed.")

        contact_records = self._get_all_contact_records(connection)
        records = self._get_all_account_records(connection, mapping, field_map)
        subform_rows = self._get_all_subform_records(connection, field_map)
        created_customers = 0
        updated_customers = 0
        visible_accounts = 0
        created_sites = 0
        linked_sites = 0
        site_conflicts = 0
        synced_at = datetime.now(UTC)
        synchronized_zoho_ids: set[str] = set()
        sites_by_domain = self._sites_by_normalized_domain()

        for record in records:
            record_id = self._as_text(record.get("id"))
            if not record_id:
                continue
            synchronized_zoho_ids.add(record_id)

            profile = self._build_profile(
                record,
                mapping,
                field_map,
                synced_at,
                subform_rows={
                    key: rows_by_account.get(record_id, [])
                    for key, rows_by_account in subform_rows.items()
                },
            )
            name = self._as_text(record.get(mapping["customer_name"])) or f"Zoho Account {record_id}"
            account_status = self._as_text(record.get(mapping["account_status"]))
            is_visible = self._is_relevant_account_status(account_status)
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
            customer.zoho_status = account_status
            customer.is_visible = is_visible
            customer.website_domain = website_domain
            customer.encrypted_profile_json = self.cipher.encrypt(json.dumps(profile, ensure_ascii=False, default=str))
            customer.zoho_modified_at = self._parse_datetime(record.get("Modified_Time"))
            customer.zoho_synced_at = synced_at
            self.db.flush()

            if is_visible:
                visible_accounts += 1
            if is_visible and website_domain:
                site_result = self._synchronize_customer_site(
                    customer=customer,
                    website_domain=website_domain,
                    sites_by_domain=sites_by_domain,
                )
                if site_result == "created":
                    created_sites += 1
                elif site_result == "linked":
                    linked_sites += 1
                elif site_result == "conflict":
                    site_conflicts += 1

        synchronized_contacts, created_contacts, updated_contacts, removed_contacts = self._synchronize_contacts(
            records=contact_records,
            synced_at=synced_at,
        )

        existing_zoho_customers = list(self.db.scalars(select(Customer).where(Customer.zoho_id.is_not(None))).all())
        for customer in existing_zoho_customers:
            if customer.zoho_id in synchronized_zoho_ids:
                continue
            customer.zoho_status = None
            customer.is_visible = False

        hidden_customers = sum(not customer.is_visible for customer in existing_zoho_customers)

        connection.last_sync_at = synced_at
        connection.last_error = None
        self.db.flush()
        return ZohoSyncResult(
            created_customers=created_customers,
            updated_customers=updated_customers,
            synchronized_accounts=len(records),
            visible_accounts=visible_accounts,
            hidden_customers=hidden_customers,
            created_sites=created_sites,
            linked_sites=linked_sites,
            site_conflicts=site_conflicts,
            synchronized_contacts=synchronized_contacts,
            created_contacts=created_contacts,
            updated_contacts=updated_contacts,
            removed_contacts=removed_contacts,
            unmapped_fields=tuple(row.label for row in mapping_rows if row.api_name is None),
        )

    def list_account_notes(self, account_id: str) -> list[dict[str, object]]:
        """Return the Zoho notes attached to one Account, newest data included."""
        connection = self._require_communication_connection()
        records: list[dict[str, object]] = []
        for page in range(1, _MAX_PAGE_REQUESTS + 1):
            response = self._api_get(
                connection,
                f"/crm/v8/{ZOHO_ACCOUNT_MODULE}/{account_id}/Notes",
                {
                    "fields": "id,Note_Title,Note_Content,Created_Time,Modified_Time,Created_By,Modified_By",
                    "per_page": "200",
                    "page": str(page),
                },
                allow_empty_response=True,
            )
            data = response.get("data")
            if not isinstance(data, list):
                raise ZohoCrmError("Zoho returned an invalid Notes response.")
            records.extend(item for item in data if isinstance(item, dict))
            info = response.get("info")
            if not (isinstance(info, dict) and info.get("more_records") is True):
                break
        return records

    def list_record_email_headers(self, module: str, record_id: str) -> list[dict[str, object]]:
        """Return up to 100 email headers for an Account or Contact related list."""
        if module not in {ZOHO_ACCOUNT_MODULE, ZOHO_CONTACT_MODULE}:
            raise ZohoCrmError("Zoho email history is only supported for Accounts and Contacts.")
        connection = self._require_communication_connection()
        records: list[dict[str, object]] = []
        next_index = "0"
        seen_indexes: set[str] = set()
        for _ in range(_MAX_PAGE_REQUESTS):
            response = self._api_get(
                connection,
                f"/crm/v8/{module}/{record_id}/Emails",
                {"index": next_index} if next_index != "0" else {},
                allow_empty_response=True,
            )
            data = response.get("Emails")
            if not isinstance(data, list):
                raise ZohoCrmError("Zoho returned an invalid email history response.")
            records.extend(item for item in data if isinstance(item, dict))
            info = response.get("info")
            following_index = info.get("next_index") if isinstance(info, dict) else None
            if not isinstance(following_index, str) or not following_index or following_index in seen_indexes:
                break
            seen_indexes.add(next_index)
            next_index = following_index
        return records

    def list_allowed_from_addresses(self) -> list[dict[str, object]]:
        """Return only the sender addresses that Zoho permits for the connected user."""
        connection = self._require_communication_connection()
        response = self._api_get(
            connection,
            "/crm/v8/settings/emails/actions/from_addresses",
            {},
        )
        addresses = response.get("from_addresses")
        if not isinstance(addresses, list):
            raise ZohoCrmError("Zoho returned no allowed sender addresses.")
        return [address for address in addresses if isinstance(address, dict)]

    def list_email_templates(self) -> list[dict[str, object]]:
        """List every email template visible in the connected Zoho organization."""
        connection = self._require_template_connection()
        templates_by_id: dict[str, dict[str, object]] = {}
        for page in range(1, _MAX_PAGE_REQUESTS + 1):
            response = self._api_get(
                connection,
                "/crm/v8/settings/email_templates",
                {"per_page": "200", "page": str(page)},
            )
            for template in self._email_template_records(response):
                template_id = self._as_text(template.get("id"))
                if template_id:
                    templates_by_id[template_id] = template
            info = response.get("info")
            if not isinstance(info, dict) or info.get("more_records") is not True:
                break
        else:
            raise ZohoCrmError(
                "Zoho returned more than 2,000 email templates. Reduce the template count before synchronizing again."
            )
        return list(templates_by_id.values())

    def get_email_template(self, *, template_id: str) -> dict[str, object]:
        """Load one template body on demand so it is never persisted as configuration."""
        normalized_template_id = self._required_template_id(template_id)
        connection = self._require_template_connection()
        response = self._api_get(
            connection,
            f"/crm/v8/settings/email_templates/{normalized_template_id}",
            {},
        )
        templates = self._email_template_records(response)
        template = next((item for item in templates if self._as_text(item.get("id")) == normalized_template_id), None)
        if template is None:
            raise ZohoCrmError("Zoho returned no matching email template.")
        return template

    def get_record_email(
        self,
        *,
        module: str,
        record_id: str,
        message_id: str,
        user_id: str | None = None,
    ) -> dict[str, object]:
        """Load a single email body only when the user asks to view it."""
        if module not in {ZOHO_ACCOUNT_MODULE, ZOHO_CONTACT_MODULE}:
            raise ZohoCrmError("Zoho email history is only supported for Accounts and Contacts.")
        connection = self._require_communication_connection()
        response = self._api_get(
            connection,
            f"/crm/v8/{module}/{record_id}/Emails/{message_id}",
            {"user_id": user_id} if user_id else {},
        )
        return self._response_record(response, "email")

    def download_record_email_attachment(
        self,
        *,
        module: str,
        record_id: str,
        message_id: str,
        user_id: str,
        attachment_id: str,
        filename: str,
    ) -> ZohoBinaryDownload:
        """Download one email attachment without exposing Zoho credentials to the browser."""
        if module not in {ZOHO_ACCOUNT_MODULE, ZOHO_CONTACT_MODULE}:
            raise ZohoCrmError("Zoho email attachments are only supported for Accounts and Contacts.")
        connection = self._require_communication_connection()
        return self._api_download(
            connection,
            f"/crm/v8/{module}/{record_id}/Emails/actions/download_attachments",
            {
                "message_id": message_id,
                "user_id": user_id,
                "id": attachment_id,
                "name": filename,
            },
        )

    def download_record_email_inline_image(
        self,
        *,
        module: str,
        record_id: str,
        message_id: str,
        user_id: str,
        image_id: str,
    ) -> ZohoBinaryDownload:
        """Download an inline image referenced by ``crm\\img_id`` in an email body."""
        if module not in {ZOHO_ACCOUNT_MODULE, ZOHO_CONTACT_MODULE}:
            raise ZohoCrmError("Zoho inline images are only supported for Accounts and Contacts.")
        connection = self._require_communication_connection()
        return self._api_download(
            connection,
            f"/crm/v8/{module}/{record_id}/Emails/actions/download_inline_images",
            {
                "message_id": message_id,
                "user_id": user_id,
                "id": image_id,
            },
        )

    def create_account_note(self, *, account_id: str, title: str, content: str) -> dict[str, object]:
        """Create a Hub-authored note in the Zoho Account related list."""
        connection = self._require_communication_connection()
        response = self._api_post_json(
            connection,
            f"/crm/v8/{ZOHO_ACCOUNT_MODULE}/{account_id}/Notes",
            {"data": [{"Note_Title": title, "Note_Content": content}]},
        )
        return self._response_record(response, "note")

    def update_note(self, *, note_id: str, title: str, content: str) -> dict[str, object]:
        """Update one existing Zoho note by its immutable CRM ID."""
        connection = self._require_communication_connection()
        response = self._api_put_json(
            connection,
            f"/crm/v8/Notes/{note_id}",
            {"data": [{"Note_Title": title, "Note_Content": content}]},
        )
        return self._response_record(response, "note update")

    def delete_note(self, *, note_id: str) -> None:
        """Delete one note in Zoho before removing its local mirror."""
        connection = self._require_communication_connection()
        response = self._api_delete(connection, f"/crm/v8/Notes/{note_id}")
        self._response_record(response, "note deletion")

    def send_account_email(
        self,
        *,
        account_id: str,
        sender_name: str,
        sender_email: str,
        recipient_name: str,
        recipient_email: str,
        subject: str,
        content: str,
        template_id: str | None = None,
        reply_to_message_id: str | None = None,
        reply_to_owner_id: str | None = None,
        cc_recipients: tuple[tuple[str, str], ...] = (),
        attachment_ids: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """Send an explicit Hub message through the sender connected to Zoho CRM."""
        connection = self._require_communication_connection()
        message: dict[str, object] = {
            "from": {"user_name": sender_name, "email": sender_email},
            "to": [{"user_name": recipient_name, "email": recipient_email}],
            "subject": subject,
            "content": content,
            "mail_format": "html",
        }
        if template_id:
            message["template"] = {"id": self._required_template_id(template_id)}
        if cc_recipients:
            message["cc"] = [
                {"user_name": name, "email": email}
                for name, email in cc_recipients
            ]
        if attachment_ids:
            message["attachments"] = [{"id": attachment_id} for attachment_id in attachment_ids]
        if reply_to_message_id:
            in_reply_to: dict[str, object] = {"message_id": reply_to_message_id}
            if reply_to_owner_id:
                in_reply_to["owner"] = {"id": reply_to_owner_id}
            message["in_reply_to"] = in_reply_to
        response = self._api_post_json(
            connection,
            f"/crm/v8/{ZOHO_ACCOUNT_MODULE}/{account_id}/actions/send_mail",
            {"data": [message]},
        )
        return self._response_record(response, "email")

    def upload_file_to_zfs(self, *, filename: str, content: bytes, content_type: str) -> str:
        """Upload one forwarded attachment and return Zoho's encrypted file ID."""
        if not content:
            raise ZohoCrmError("Ein leerer Anhang kann nicht weitergeleitet werden.")
        if len(content) > 20 * 1024 * 1024:
            raise ZohoCrmError("Ein weitergeleiteter Anhang überschreitet das Zoho-Limit von 20 MB.")
        normalized_filename = re.sub(r"[\r\n\"]", "_", filename).strip() or "attachment"
        normalized_content_type = content_type.casefold()
        if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", normalized_content_type):
            normalized_content_type = "application/octet-stream"
        connection = self._require_communication_connection()
        if not self._has_scope_grant(connection, frozenset({_ZOHO_FILE_SCOPE})):
            raise ZohoCrmError("Reconnect Zoho CRM once to approve access for forwarded attachments.")
        boundary = f"----KosmosHub{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{normalized_filename}"\r\n'
            f"Content-Type: {normalized_content_type}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        response = self._api_post_multipart(
            connection,
            "/crm/v8/files",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        result = self._response_record(response, "file")
        details = result.get("details") if isinstance(result.get("details"), dict) else {}
        file_id = self._text(details.get("id")) or self._text(result.get("id"))
        if not file_id:
            raise ZohoCrmError("Zoho returned no encrypted file ID for the forwarded attachment.")
        return file_id

    def remove_connection(self, *, actor: HubUser) -> None:
        self._require_admin(actor)
        connection = self.get_connection()
        if connection is None:
            raise ZohoCrmError("No Zoho CRM connection is stored in the Hub.")
        self._clear_cached_access_token(connection)
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
        available_by_api_name: set[str] = set()
        for field in fields:
            if not isinstance(field, dict):
                continue
            api_name = field.get("api_name")
            if not isinstance(api_name, str) or not api_name.strip():
                continue
            available_by_api_name.add(api_name)
            for candidate in (field.get("field_label"), field.get("display_label"), api_name):
                normalized = ZohoCrmService._normalize_field_label(candidate)
                if normalized:
                    available.setdefault(normalized, set()).add(api_name)

        mapped: dict[str, str | None] = {}
        for field in ZOHO_ACCOUNT_FIELDS:
            if field.key == "record_id":
                mapped[field.key] = "id"
                continue
            if field.api_name in available_by_api_name:
                mapped[field.key] = field.api_name
                continue
            matches = available.get(ZohoCrmService._normalize_field_label(field.label), set())
            mapped[field.key] = next(iter(matches)) if len(matches) == 1 else None
        return {"module": ZOHO_ACCOUNT_MODULE, "fields": mapped}

    @classmethod
    def _field_metadata_by_key(
        cls,
        definitions: tuple[ZohoAccountField, ...],
        raw_fields: list[object],
        mapped_fields: object,
    ) -> dict[str, dict[str, object]]:
        raw_by_api_name = {
            api_name: field
            for field in raw_fields
            if isinstance(field, dict)
            and isinstance((api_name := field.get("api_name")), str)
            and api_name
        }
        mapping = mapped_fields if isinstance(mapped_fields, dict) else {}
        return {
            definition.key: cls._serialize_field_metadata(
                definition,
                raw_by_api_name.get(cls._as_text(mapping.get(definition.key)) or ""),
            )
            for definition in definitions
        }

    def _subform_field_maps(self, connection: ZohoConnection, account_fields: list[object]) -> dict[str, dict[str, object]]:
        account_by_api_name = {
            api_name: field
            for field in account_fields
            if isinstance(field, dict)
            and isinstance((api_name := field.get("api_name")), str)
            and api_name
        }
        subforms: dict[str, dict[str, object]] = {}
        for subform in ZOHO_ACCOUNT_SUBFORMS:
            parent = account_by_api_name.get(subform.parent_api_name)
            associated_module = parent.get("associated_module") if isinstance(parent, dict) else None
            module_name = self._as_text(associated_module.get("module")) if isinstance(associated_module, dict) else None
            module_name = module_name or subform.parent_api_name
            response = self._api_get(connection, "/crm/v8/settings/fields", {"module": module_name})
            raw_fields = response.get("fields")
            if not isinstance(raw_fields, list):
                raise ZohoCrmError(f"Zoho returned no field metadata for the subform {subform.label}.")
            field_mapping = self.resolve_subform_field_mapping(subform.fields, raw_fields)
            subforms[subform.key] = {
                "label": subform.label,
                "parent_api_name": subform.parent_api_name,
                "module": module_name,
                "fields": field_mapping,
                "metadata": self._field_metadata_by_key(subform.fields, raw_fields, field_mapping),
            }
        return subforms

    @staticmethod
    def resolve_subform_field_mapping(definitions: tuple[ZohoAccountField, ...], fields: list[object]) -> dict[str, str | None]:
        available_by_api_name = {
            api_name
            for field in fields
            if isinstance(field, dict)
            and isinstance((api_name := field.get("api_name")), str)
            and api_name
        }
        labels: dict[str, set[str]] = {}
        for field in fields:
            if not isinstance(field, dict):
                continue
            api_name = field.get("api_name")
            if not isinstance(api_name, str):
                continue
            for candidate in (field.get("field_label"), field.get("display_label"), api_name):
                normalized = ZohoCrmService._normalize_field_label(candidate)
                if normalized:
                    labels.setdefault(normalized, set()).add(api_name)
        mapping: dict[str, str | None] = {}
        for definition in definitions:
            if definition.api_name in available_by_api_name:
                mapping[definition.key] = definition.api_name
                continue
            matches = labels.get(ZohoCrmService._normalize_field_label(definition.label), set())
            mapping[definition.key] = next(iter(matches)) if len(matches) == 1 else None
        return mapping

    @classmethod
    def _serialize_field_metadata(cls, definition: ZohoAccountField, raw: object) -> dict[str, object]:
        source = raw if isinstance(raw, dict) else {}
        operation_type = source.get("operation_type")
        can_update = not source.get("read_only") and not source.get("field_read_only")
        if isinstance(operation_type, dict) and operation_type.get("api_update") is False:
            can_update = False
        pick_list_values: list[dict[str, str]] = []
        raw_options = source.get("pick_list_values")
        if isinstance(raw_options, list):
            for option in raw_options:
                if not isinstance(option, dict):
                    continue
                value = cls._as_text(option.get("actual_value")) or cls._as_text(option.get("display_value"))
                if value:
                    pick_list_values.append({"value": value, "label": cls._as_text(option.get("display_value")) or value})
        return {
            "key": definition.key,
            "label": definition.label,
            "api_name": cls._as_text(source.get("api_name")),
            "display_type": definition.display_type,
            "zoho_type": cls._as_text(source.get("data_type")) or definition.display_type,
            "pick_list_values": pick_list_values,
            "editable": bool(source) and bool(can_update) and definition.key != "record_id",
            "sensitive": definition.sensitive,
            "subform_parent": definition.subform_parent,
        }

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

    def _get_all_account_records(
        self,
        connection: ZohoConnection,
        mapping: dict[str, str | None],
        field_map: dict[str, Any] | None = None,
    ) -> list[dict[str, object]]:
        requested_fields = sorted({api_name for api_name in mapping.values() if api_name and api_name != "id"} | {"Modified_Time"})
        subforms = field_map.get("subforms") if isinstance(field_map, dict) else {}
        if isinstance(subforms, dict):
            requested_fields.extend(
                parent_api_name
                for subform in subforms.values()
                if isinstance(subform, dict)
                and isinstance((parent_api_name := subform.get("parent_api_name")), str)
                and parent_api_name
            )
        requested_fields = sorted(set(requested_fields))
        records_by_id: dict[str, dict[str, object]] = {}
        for offset in range(0, len(requested_fields), _MAX_FIELDS_PER_ZOHO_REQUEST):
            field_chunk = requested_fields[offset : offset + _MAX_FIELDS_PER_ZOHO_REQUEST]
            for page in range(1, _MAX_PAGE_REQUESTS + 1):
                response = self._api_get(
                    connection,
                    f"/crm/v8/{ZOHO_ACCOUNT_MODULE}",
                    {"fields": ",".join(field_chunk), "per_page": "200", "page": str(page)},
                    allow_empty_response=True,
                )
                data = response.get("data")
                if not isinstance(data, list):
                    raise ZohoCrmError("Zoho returned an invalid Accounts response. No customers were changed.")
                for record in data:
                    if not isinstance(record, dict):
                        continue
                    record_id = self._as_text(record.get("id"))
                    if record_id:
                        records_by_id.setdefault(record_id, {"id": record_id}).update(record)
                info = response.get("info")
                if not (isinstance(info, dict) and info.get("more_records") is True):
                    break
            else:
                raise ZohoCrmError("Zoho returned more than 2,000 Accounts. Bulk synchronization must be enabled before importing them.")
        return list(records_by_id.values())

    def _get_all_subform_records(
        self,
        connection: ZohoConnection,
        field_map: dict[str, Any],
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        subforms = field_map.get("subforms") if isinstance(field_map, dict) else {}
        if not isinstance(subforms, dict):
            return {}
        records_by_subform: dict[str, dict[str, list[dict[str, object]]]] = {}
        for key, subform in subforms.items():
            if not isinstance(key, str) or not isinstance(subform, dict):
                continue
            module = self._as_text(subform.get("module"))
            fields = subform.get("fields")
            if not module or not isinstance(fields, dict):
                continue
            requested_fields = sorted({api_name for api_name in fields.values() if isinstance(api_name, str) and api_name} | {"Parent_Id"})
            rows_by_account: dict[str, list[dict[str, object]]] = {}
            for page in range(1, _MAX_PAGE_REQUESTS + 1):
                response = self._api_get(
                    connection,
                    f"/crm/v8/{module}",
                    {"fields": ",".join(requested_fields), "per_page": "200", "page": str(page)},
                    allow_empty_response=True,
                )
                data = response.get("data")
                if not isinstance(data, list):
                    raise ZohoCrmError(f"Zoho returned an invalid {subform.get('label', key)} response.")
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    parent_id = self._lookup_record_id(row.get("Parent_Id"))
                    if parent_id:
                        rows_by_account.setdefault(parent_id, []).append(row)
                info = response.get("info")
                if not (isinstance(info, dict) and info.get("more_records") is True):
                    break
            else:
                raise ZohoCrmError(f"Zoho returned too many {subform.get('label', key)} rows for a regular synchronization.")
            records_by_subform[key] = rows_by_account
        return records_by_subform

    def _synchronize_contacts(
        self,
        *,
        records: list[dict[str, object]],
        synced_at: datetime,
        remove_missing: bool = True,
    ) -> tuple[int, int, int, int]:
        """Import Contacts only when their Zoho Account is also a Hub customer."""
        customers_by_zoho_id = {
            customer.zoho_id: customer
            for customer in self.db.scalars(select(Customer).where(Customer.zoho_id.is_not(None))).all()
            if customer.zoho_id
        }
        synchronized_contacts = 0
        created_contacts = 0
        updated_contacts = 0
        linked_contact_ids: set[str] = set()

        for record in records:
            synchronized_contacts += 1
            contact_id = self._as_text(record.get("id"))
            account_id = self._lookup_record_id(record.get("Account_Name"))
            customer = customers_by_zoho_id.get(account_id or "")
            if not contact_id or customer is None:
                continue

            linked_contact_ids.add(contact_id)
            contact = self.db.scalar(select(CustomerContact).where(CustomerContact.zoho_id == contact_id))
            if contact is None:
                contact = CustomerContact(
                    customer=customer,
                    zoho_id=contact_id,
                    encrypted_profile_json="",
                )
                self.db.add(contact)
                created_contacts += 1
            else:
                contact.customer = customer
                updated_contacts += 1

            contact.encrypted_profile_json = self.cipher.encrypt(
                json.dumps(self._build_contact_profile(record, account_id, synced_at), ensure_ascii=False, default=str)
            )
            contact.zoho_modified_at = self._parse_datetime(record.get("Modified_Time"))
            contact.zoho_synced_at = synced_at

        removed_contacts = 0
        if remove_missing:
            for contact in self.db.scalars(select(CustomerContact)).all():
                if contact.zoho_id in linked_contact_ids:
                    continue
                self.db.delete(contact)
                removed_contacts += 1

        self.db.flush()
        return synchronized_contacts, created_contacts, updated_contacts, removed_contacts

    def synchronize_all_contacts(self) -> ZohoContactSyncResult:
        """Refresh every Zoho Contact linked to an existing Hub customer."""
        connection = self._require_connected_connection()
        synced_at = datetime.now(UTC)
        synchronized, created, updated, removed = self._synchronize_contacts(
            records=self._get_all_contact_records(connection),
            synced_at=synced_at,
            remove_missing=False,
        )
        connection.last_error = None
        self.db.flush()
        return ZohoContactSyncResult(
            synchronized_contacts=synchronized,
            created_contacts=created,
            updated_contacts=updated,
            removed_contacts=removed,
        )

    def list_case_records(self) -> list[dict[str, object]]:
        """Return every Zoho Case needed by the reviewed Hub Fälle schema."""
        connection = self._require_connected_connection()
        requested_fields = ",".join(
            (
                "Case_Number",
                "Status",
                "Case_Reason",
                "Case_Origin",
                "Created_Time",
                "Modified_Time",
                "Description",
                "Account_Name",
                "Dauer_des_Falls_in_Minuten",
                "Betrag_in_Rechnung_gestellt_netto",
            )
        )
        records_by_id: dict[str, dict[str, object]] = {}
        for page in range(1, _MAX_CASE_PAGE_REQUESTS + 1):
            response = self._api_get(
                connection,
                f"/crm/v8/{ZOHO_CASE_MODULE}",
                {"fields": requested_fields, "per_page": "200", "page": str(page)},
                allow_empty_response=True,
            )
            data = response.get("data")
            if not isinstance(data, list):
                raise ZohoCrmError("Zoho returned an invalid Cases response. No cases were changed.")
            for record in data:
                if not isinstance(record, dict):
                    continue
                case_id = self._as_text(record.get("id"))
                if case_id:
                    records_by_id[case_id] = record
            info = response.get("info")
            if not (isinstance(info, dict) and info.get("more_records") is True):
                break
        else:
            raise ZohoCrmError("Zoho returned more than 20,000 Cases. The import was not changed.")
        return list(records_by_id.values())

    def _get_all_contact_records(self, connection: ZohoConnection) -> list[dict[str, object]]:
        requested_fields = ",".join(
            ("Account_Name", "Full_Name", "Modified_Time", *(field.api_name for field in ZOHO_CONTACT_FIELDS))
        )
        records_by_id: dict[str, dict[str, object]] = {}
        for page in range(1, _MAX_PAGE_REQUESTS + 1):
            response = self._api_get(
                connection,
                f"/crm/v8/{ZOHO_CONTACT_MODULE}",
                {
                    "fields": requested_fields,
                    "per_page": "200",
                    "page": str(page),
                },
                allow_empty_response=True,
            )
            data = response.get("data")
            if not isinstance(data, list):
                raise ZohoCrmError("Zoho returned an invalid Contacts response. No contacts were changed.")
            for record in data:
                if not isinstance(record, dict):
                    continue
                contact_id = self._as_text(record.get("id"))
                if contact_id:
                    records_by_id[contact_id] = record
            info = response.get("info")
            more_records = isinstance(info, dict) and info.get("more_records") is True
            if not more_records:
                break
        else:
            raise ZohoCrmError("Zoho returned more than 2,000 Contacts. Bulk synchronization must be enabled before importing them.")
        return list(records_by_id.values())

    def create_contact(self, *, customer_id: int, submitted_values: dict[str, str]) -> CustomerContact:
        """Create a Contact in Zoho and retain its encrypted local representation."""
        connection = self._require_connected_connection()
        customer = self.db.get(Customer, customer_id)
        if customer is None or not customer.is_visible or not customer.zoho_id:
            raise ZohoCrmError("Wähle einen aktuellen, mit Zoho verknüpften Kunden aus.")

        values = self._contact_creation_values(submitted_values)
        response = self._api_post_json(
            connection,
            f"/crm/v8/{ZOHO_CONTACT_MODULE}",
            {"data": [{"Account_Name": {"id": customer.zoho_id}, **values}]},
        )
        created_id = self._created_contact_id(response)
        synced_at = datetime.now(UTC)
        record = {"id": created_id, "Account_Name": {"id": customer.zoho_id}, **values}
        contact = CustomerContact(
            customer=customer,
            zoho_id=created_id,
            encrypted_profile_json=self.cipher.encrypt(
                json.dumps(self._build_contact_profile(record, customer.zoho_id, synced_at), ensure_ascii=False, default=str)
            ),
            zoho_synced_at=synced_at,
        )
        self.db.add(contact)
        self.db.flush()
        return contact

    def update_contact(self, *, customer_id: int, contact_id: int, submitted_values: dict[str, str]) -> CustomerContact:
        """Update a customer's Contact in Zoho and refresh the encrypted local copy."""
        connection = self._require_connected_connection()
        contact = self.db.get(CustomerContact, contact_id)
        customer = self.db.get(Customer, customer_id)
        if contact is None or customer is None or contact.customer_id != customer.id or not contact.zoho_id:
            raise ZohoCrmError("Der Kontakt konnte nicht gefunden werden.")
        values = self._contact_update_values(submitted_values)
        response = self._api_put_json(
            connection,
            f"/crm/v8/{ZOHO_CONTACT_MODULE}/{contact.zoho_id}",
            {"data": [{"id": contact.zoho_id, **values}]},
        )
        self._contact_result_id(response, action="aktualisieren")
        synced_at = datetime.now(UTC)
        record = {"id": contact.zoho_id, **values}
        contact.encrypted_profile_json = self.cipher.encrypt(
            json.dumps(self._build_contact_profile(record, customer.zoho_id, synced_at), ensure_ascii=False, default=str)
        )
        contact.zoho_synced_at = synced_at
        self.db.flush()
        return contact

    def synchronize_contact(self, *, customer_id: int, contact_id: int) -> CustomerContact:
        """Refresh one Contact without running the full CRM import."""
        connection = self._require_connected_connection()
        contact = self.db.get(CustomerContact, contact_id)
        customer = self.db.get(Customer, customer_id)
        if contact is None or customer is None or contact.customer_id != customer.id or not contact.zoho_id:
            raise ZohoCrmError("Der Kontakt konnte nicht gefunden werden.")
        fields = ",".join(("Account_Name", "Full_Name", "Modified_Time", *(field.api_name for field in ZOHO_CONTACT_FIELDS)))
        response = self._api_get(connection, f"/crm/v8/{ZOHO_CONTACT_MODULE}/{contact.zoho_id}", {"fields": fields})
        data = response.get("data")
        record = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if record is None:
            raise ZohoCrmError("Zoho returned no usable Contact data.")
        synced_at = datetime.now(UTC)
        contact.encrypted_profile_json = self.cipher.encrypt(
            json.dumps(self._build_contact_profile(record, customer.zoho_id, synced_at), ensure_ascii=False, default=str)
        )
        contact.zoho_modified_at = self._parse_datetime(record.get("Modified_Time"))
        contact.zoho_synced_at = synced_at
        self.db.flush()
        return contact

    @staticmethod
    def _created_contact_id(response: dict[str, object]) -> str:
        return ZohoCrmService._contact_result_id(response, action="erstellen")

    @staticmethod
    def _contact_result_id(response: dict[str, object], *, action: str) -> str:
        data = response.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ZohoCrmError(f"Zoho returned no Contact result while trying to {action} it.")
        result = data[0]
        status = ZohoCrmService._as_text(result.get("status"))
        code = ZohoCrmService._as_text(result.get("code"))
        if (status and status.casefold() != "success") or (code and code.casefold() != "success"):
            raise ZohoCrmError(ZohoCrmService._as_text(result.get("message")) or f"Zoho could not {action} the Contact.")
        details = result.get("details")
        contact_id = ZohoCrmService._as_text(details.get("id")) if isinstance(details, dict) else None
        if not contact_id:
            raise ZohoCrmError("Zoho returned no usable Contact ID.")
        return contact_id

    @staticmethod
    def _contact_creation_values(submitted_values: dict[str, str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for field in ZOHO_CONTACT_FIELDS:
            raw_value = submitted_values.get(f"contact_field__{field.key}", "")
            value = raw_value.strip()
            if len(value) > _MAX_CONTACT_FIELD_LENGTH:
                raise ZohoCrmError(f"{field.label} ist zu lang.")
            if (field.required or field.key == "salutation") and not value:
                raise ZohoCrmError(f"{field.label} ist erforderlich.")
            if value:
                values[field.api_name] = value
        return values

    @staticmethod
    def _contact_update_values(submitted_values: dict[str, str]) -> dict[str, str | None]:
        values: dict[str, str | None] = {}
        for field in ZOHO_CONTACT_FIELDS:
            value = submitted_values.get(f"contact_field__{field.key}", "").strip()
            if len(value) > _MAX_CONTACT_FIELD_LENGTH:
                raise ZohoCrmError(f"{field.label} ist zu lang.")
            if field.required and not value:
                raise ZohoCrmError(f"{field.label} ist erforderlich.")
            values[field.api_name] = value or None
        return values

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

    def _api_post_json(
        self,
        connection: ZohoConnection,
        path: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        access_token = self._refresh_access_token(connection)
        data_center = self._data_center(connection.data_center)
        api_domain = connection.api_domain or data_center.api_domain
        return self._request_json(
            f"{api_domain}{path}",
            method="POST",
            json_body=payload,
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )

    def _api_put_json(
        self,
        connection: ZohoConnection,
        path: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        access_token = self._refresh_access_token(connection)
        data_center = self._data_center(connection.data_center)
        api_domain = connection.api_domain or data_center.api_domain
        return self._request_json(
            f"{api_domain}{path}",
            method="PUT",
            json_body=payload,
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )

    def _api_delete(self, connection: ZohoConnection, path: str) -> dict[str, object]:
        access_token = self._refresh_access_token(connection)
        data_center = self._data_center(connection.data_center)
        api_domain = connection.api_domain or data_center.api_domain
        return self._request_json(
            f"{api_domain}{path}",
            method="DELETE",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )

    def _api_post_multipart(
        self,
        connection: ZohoConnection,
        path: str,
        *,
        body: bytes,
        content_type: str,
    ) -> dict[str, object]:
        access_token = self._refresh_access_token(connection)
        data_center = self._data_center(connection.data_center)
        api_domain = connection.api_domain or data_center.api_domain
        return self._request_json(
            f"{api_domain}{path}",
            method="POST",
            raw_body=body,
            content_type=content_type,
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )

    def _api_download(
        self,
        connection: ZohoConnection,
        path: str,
        params: dict[str, str],
    ) -> ZohoBinaryDownload:
        access_token = self._refresh_access_token(connection)
        data_center = self._data_center(connection.data_center)
        api_domain = connection.api_domain or data_center.api_domain
        return self._request_binary(
            f"{api_domain}{path}?{urlencode(params)}",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )

    def _refresh_access_token(self, connection: ZohoConnection) -> str:
        cache_key = self._access_token_cache_key(connection)
        now = datetime.now(UTC)
        with _ACCESS_TOKEN_CACHE_LOCK:
            cached = _ACCESS_TOKEN_CACHE.get(cache_key)
            if cached is not None and cached[1] > now:
                return cached[0]

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
        lifetime_seconds = self._access_token_lifetime_seconds(response)
        expires_at = now + timedelta(seconds=max(1, lifetime_seconds - _ACCESS_TOKEN_EXPIRY_BUFFER_SECONDS))
        with _ACCESS_TOKEN_CACHE_LOCK:
            _ACCESS_TOKEN_CACHE[cache_key] = (access_token, expires_at)
        return access_token

    @staticmethod
    def _access_token_lifetime_seconds(response: dict[str, object]) -> int:
        for key in ("expires_in_sec", "expires_in"):
            value = response.get(key)
            try:
                lifetime = int(str(value))
            except (TypeError, ValueError):
                continue
            if lifetime > 0:
                return lifetime
        return _DEFAULT_ACCESS_TOKEN_LIFETIME_SECONDS

    @staticmethod
    def _access_token_cache_key(connection: ZohoConnection) -> str:
        return f"{connection.id or 'new'}:{connection.encrypted_refresh_token or ''}"

    @staticmethod
    def _clear_cached_access_token(connection: ZohoConnection) -> None:
        prefix = f"{connection.id or 'new'}:"
        with _ACCESS_TOKEN_CACHE_LOCK:
            for cache_key in tuple(_ACCESS_TOKEN_CACHE):
                if cache_key.startswith(prefix):
                    del _ACCESS_TOKEN_CACHE[cache_key]

    def _build_profile(
        self,
        record: dict[str, object],
        mapping: dict[str, str | None],
        field_map: dict[str, Any],
        synced_at: datetime,
        *,
        subform_rows: dict[str, list[dict[str, object]]] | None = None,
    ) -> dict[str, object]:
        profile: dict[str, object] = {
            "source": "zoho-crm-accounts",
            "schema_version": 2,
            "record_id": self._as_text(record.get("id")),
            "modified_time": self._as_text(record.get("Modified_Time")),
            "synced_at": synced_at.isoformat(),
            "fields": {},
            "field_metadata": field_map.get("metadata", {}) if isinstance(field_map, dict) else {},
            "subforms": {},
        }
        values = profile["fields"]
        assert isinstance(values, dict)
        for field in ZOHO_ACCOUNT_FIELDS:
            if field.subform_parent:
                continue
            api_name = mapping.get(field.key)
            values[field.label] = record.get(api_name) if api_name else None
        stored_subforms = profile["subforms"]
        assert isinstance(stored_subforms, dict)
        configured_subforms = field_map.get("subforms") if isinstance(field_map, dict) else {}
        if not isinstance(configured_subforms, dict):
            return profile
        for subform in ZOHO_ACCOUNT_SUBFORMS:
            definition = configured_subforms.get(subform.key)
            if not isinstance(definition, dict):
                continue
            subform_mapping = definition.get("fields")
            if not isinstance(subform_mapping, dict):
                continue
            raw_rows = (subform_rows or {}).get(subform.key)
            if raw_rows is None:
                parent_api_name = self._as_text(definition.get("parent_api_name"))
                nested_rows = record.get(parent_api_name) if parent_api_name else None
                raw_rows = [row for row in nested_rows if isinstance(row, dict)] if isinstance(nested_rows, list) else []
            rows: list[dict[str, object]] = []
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    continue
                rows.append(
                    {
                        "id": self._as_text(raw_row.get("id")),
                        "values": {
                            field.key: raw_row.get(api_name)
                            for field in subform.fields
                            if isinstance((api_name := subform_mapping.get(field.key)), str) and api_name
                        },
                    }
                )
            stored_subforms[subform.key] = {
                "label": definition.get("label", subform.label),
                "parent_api_name": definition.get("parent_api_name", subform.parent_api_name),
                "metadata": definition.get("metadata", {}),
                "records": rows,
            }
        return profile

    def update_customer_fields(self, *, customer_id: int, submitted_values: dict[str, str]) -> Customer:
        """Write permitted Account and subform changes to Zoho, then refresh the encrypted Hub profile."""
        connection = self._require_connected_connection()
        customer = self.db.get(Customer, customer_id)
        if customer is None or not customer.zoho_id:
            raise ZohoCrmError("This customer is not linked to a Zoho Account.")
        profile = self._customer_profile(customer)
        metadata = profile.get("field_metadata")
        if not isinstance(metadata, dict):
            raise ZohoCrmError("Synchronize the customer data once before editing Zoho fields.")

        changes = self._root_field_changes(metadata, profile, submitted_values)
        changes.update(self._subform_changes(profile, submitted_values))
        if not changes:
            return customer

        response = self._api_put_json(
            connection,
            f"/crm/v8/{ZOHO_ACCOUNT_MODULE}/{customer.zoho_id}",
            {"data": [{"id": customer.zoho_id, **changes}]},
        )
        result = self._response_record(response, "Account update")
        code = self._as_text(result.get("code"))
        if code and code.casefold() != "success":
            raise ZohoCrmError(self._as_text(result.get("message")) or "Zoho could not save the Account fields.")

        refreshed = self._api_get(
            connection,
            f"/crm/v8/{ZOHO_ACCOUNT_MODULE}/{customer.zoho_id}",
            {},
        )
        data = refreshed.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ZohoCrmError("Zoho saved the Account but did not return the refreshed data.")
        field_map = self._stored_field_map(connection)
        mapping = field_map.get("fields") if isinstance(field_map.get("fields"), dict) else {}
        synced_at = datetime.now(UTC)
        self._apply_customer_record(customer, data[0], mapping, field_map, synced_at)
        connection.last_sync_at = synced_at
        connection.last_error = None
        self.db.flush()
        return customer

    def _root_field_changes(
        self,
        metadata: dict[str, object],
        profile: dict[str, object],
        submitted_values: dict[str, str],
    ) -> dict[str, object]:
        stored_values = profile.get("fields") if isinstance(profile.get("fields"), dict) else {}
        changes: dict[str, object] = {}
        for field in ZOHO_ACCOUNT_FIELDS:
            if field.key == "record_id" or field.subform_parent:
                continue
            definition = metadata.get(field.key)
            if not isinstance(definition, dict) or not definition.get("editable"):
                continue
            input_name = f"customer_field__{field.key}"
            if input_name not in submitted_values and field.display_type != "Boolesch":
                continue
            api_name = self._as_text(definition.get("api_name"))
            if not api_name:
                continue
            submitted = submitted_values.get(input_name, "false" if field.display_type == "Boolesch" else "")
            if field.sensitive and not submitted.strip():
                continue
            value = self._normalize_field_value(submitted, definition)
            previous = stored_values.get(field.label) if isinstance(stored_values, dict) else None
            if self._profile_value_key(previous) != self._profile_value_key(value):
                changes[api_name] = value
        return changes

    def _subform_changes(self, profile: dict[str, object], submitted_values: dict[str, str]) -> dict[str, object]:
        stored_subforms = profile.get("subforms")
        if not isinstance(stored_subforms, dict):
            return {}
        changes: dict[str, object] = {}
        for subform_key, source in stored_subforms.items():
            if not isinstance(subform_key, str) or not isinstance(source, dict):
                continue
            parent_api_name = self._as_text(source.get("parent_api_name"))
            metadata = source.get("metadata")
            records = source.get("records")
            if not parent_api_name or not isinstance(metadata, dict) or not isinstance(records, list):
                continue
            rows: list[dict[str, object]] = []
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    continue
                row_id = self._as_text(record.get("id"))
                if not row_id:
                    continue
                prefix = f"customer_subform__{subform_key}__{index}"
                if submitted_values.get(f"{prefix}__delete") == "true":
                    rows.append({"id": row_id, "_delete": None})
                    continue
                row_values = record.get("values") if isinstance(record.get("values"), dict) else {}
                changed_row: dict[str, object] = {"id": row_id}
                for field_key, definition in metadata.items():
                    if not isinstance(field_key, str) or not isinstance(definition, dict) or not definition.get("editable"):
                        continue
                    input_name = f"{prefix}__{field_key}"
                    if input_name not in submitted_values:
                        continue
                    submitted = submitted_values[input_name]
                    if definition.get("sensitive") and not submitted.strip():
                        continue
                    value = self._normalize_field_value(submitted, definition)
                    if self._profile_value_key(row_values.get(field_key)) != self._profile_value_key(value):
                        api_name = self._as_text(definition.get("api_name"))
                        if api_name:
                            changed_row[api_name] = value
                if len(changed_row) > 1:
                    rows.append(changed_row)

            new_prefix = f"customer_subform__{subform_key}__new"
            new_row: dict[str, object] = {}
            for field_key, definition in metadata.items():
                if not isinstance(field_key, str) or not isinstance(definition, dict) or not definition.get("editable"):
                    continue
                submitted = submitted_values.get(f"{new_prefix}__{field_key}", "")
                if not submitted.strip():
                    continue
                api_name = self._as_text(definition.get("api_name"))
                if api_name:
                    new_row[api_name] = self._normalize_field_value(submitted, definition)
            if new_row:
                rows.append(new_row)
            if rows:
                changes[parent_api_name] = rows
        return changes

    @staticmethod
    def _normalize_field_value(value: str, definition: dict[str, object]) -> object:
        normalized = value.strip()
        if definition.get("display_type") == "Boolesch":
            return normalized.casefold() in {"true", "1", "on", "yes"}
        options = definition.get("pick_list_values")
        if isinstance(options, list) and normalized:
            allowed = {
                option.get("value")
                for option in options
                if isinstance(option, dict) and isinstance(option.get("value"), str)
            }
            if allowed and normalized not in allowed:
                raise ZohoCrmError("Choose a current value from the Zoho selection list.")
        return normalized or None

    @staticmethod
    def _profile_value_key(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value).strip()

    def _customer_profile(self, customer: Customer) -> dict[str, object]:
        if not customer.encrypted_profile_json:
            return {}
        try:
            parsed = json.loads(self.cipher.decrypt(customer.encrypted_profile_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ZohoCrmError("The stored Zoho customer profile cannot be read.") from exc
        return parsed if isinstance(parsed, dict) else {}

    def _apply_customer_record(
        self,
        customer: Customer,
        record: dict[str, object],
        mapping: dict[str, object],
        field_map: dict[str, Any],
        synced_at: datetime,
    ) -> None:
        profile_mapping = {key: self._as_text(value) for key, value in mapping.items() if isinstance(key, str)}
        profile = self._build_profile(record, profile_mapping, field_map, synced_at)
        customer.name = self._as_text(record.get(profile_mapping.get("customer_name"))) or f"Zoho Account {customer.zoho_id}"
        customer.external_id = self._as_text(record.get(profile_mapping.get("customer_number")))
        customer.zoho_status = self._as_text(record.get(profile_mapping.get("account_status")))
        customer.is_visible = self._is_relevant_account_status(customer.zoho_status)
        customer.website_domain = self.normalize_website_domain(record.get(profile_mapping.get("website")))
        customer.encrypted_profile_json = self.cipher.encrypt(json.dumps(profile, ensure_ascii=False, default=str))
        customer.zoho_modified_at = self._parse_datetime(record.get("Modified_Time"))
        customer.zoho_synced_at = synced_at

    def _build_contact_profile(
        self,
        record: dict[str, object],
        account_id: str | None,
        synced_at: datetime,
    ) -> dict[str, object]:
        name = self._as_text(record.get("Full_Name"))
        if not name:
            name = " ".join(
                value
                for value in (self._as_text(record.get("First_Name")), self._as_text(record.get("Last_Name")))
                if value
            )
        fields = {"Name": name or "Zoho Contact"}
        fields.update(
            {
                field.label: self._as_text(record.get(field.api_name))
                for field in ZOHO_CONTACT_FIELDS
            }
        )
        return {
            "source": "zoho-crm-contacts",
            "record_id": self._as_text(record.get("id")),
            "account_id": account_id,
            "modified_time": self._as_text(record.get("Modified_Time")),
            "synced_at": synced_at.isoformat(),
            "fields": fields,
        }

    def _sites_by_normalized_domain(self) -> dict[str, list[Site]]:
        sites_by_domain: dict[str, list[Site]] = {}
        for site in self.db.scalars(select(Site)).all():
            domain = self.normalize_website_domain(site.domain)
            if domain:
                sites_by_domain.setdefault(domain, []).append(site)
        return sites_by_domain

    def _synchronize_customer_site(
        self,
        *,
        customer: Customer,
        website_domain: str,
        sites_by_domain: dict[str, list[Site]],
    ) -> str:
        """Create or safely reuse the Hub record for a current Zoho customer site."""
        candidates = sites_by_domain.get(website_domain, [])
        if any(site.customer_id == customer.id for site in candidates):
            return "existing"

        if len(candidates) == 1 and candidates[0].customer_id is None:
            candidates[0].customer_id = customer.id
            return "linked"

        if candidates:
            return "conflict"

        home_url = f"https://{website_domain}/"
        site = Site(
            uuid=str(uuid.uuid4()),
            customer_id=customer.id,
            domain=website_domain,
            home_url=home_url,
            site_url=home_url,
            status=SiteStatus.pending.value,
        )
        self.db.add(site)
        self.db.flush()
        sites_by_domain[website_domain] = [site]
        return "created"

    @staticmethod
    def _lookup_record_id(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        return ZohoCrmService._as_text(value.get("id"))

    @staticmethod
    def _is_relevant_account_status(value: object) -> bool:
        status = ZohoCrmService._as_text(value)
        return status is not None and status.casefold() in {item.casefold() for item in ZOHO_RELEVANT_ACCOUNT_STATUSES}

    @staticmethod
    def _has_current_scope_grant(connection: ZohoConnection) -> bool:
        return ZohoCrmService._has_scope_grant(connection, frozenset(_ZOHO_CRM_SCOPE_VALUES))

    @staticmethod
    def _has_scope_grant(connection: ZohoConnection, required_scopes: frozenset[str]) -> bool:
        granted_scopes = {scope.strip() for scope in connection.scopes.split(",")}
        return required_scopes.issubset(granted_scopes)

    @staticmethod
    def _utc_datetime(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

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

    def _require_communication_connection(self) -> ZohoConnection:
        connection = self._require_connected_connection()
        if not self._has_scope_grant(connection, _ZOHO_COMMUNICATION_SCOPE_VALUES):
            raise ZohoCrmError("Reconnect Zoho CRM once to approve the requested email and note access.")
        return connection

    def _require_template_connection(self) -> ZohoConnection:
        connection = self._require_connected_connection()
        if not self._has_scope_grant(connection, frozenset({_ZOHO_EMAIL_TEMPLATE_SCOPE})):
            raise ZohoCrmError("Reconnect Zoho CRM once to approve access to Zoho email templates.")
        return connection

    @staticmethod
    def _email_template_records(response: dict[str, object]) -> list[dict[str, object]]:
        records = response.get("email_templates") or response.get("data")
        if not isinstance(records, list):
            raise ZohoCrmError("Zoho returned an invalid email template response.")
        return [record for record in records if isinstance(record, dict)]

    @staticmethod
    def _required_template_id(value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", normalized):
            raise ZohoCrmError("Choose a valid Zoho email template.")
        return normalized

    @staticmethod
    def _response_record(response: dict[str, object], label: str) -> dict[str, object]:
        data = response.get("data") or response.get("Emails")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ZohoCrmError(f"Zoho returned no {label} result.")
        result = data[0]
        details = result.get("details")
        if isinstance(details, dict):
            return details
        return result

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
        json_body: dict[str, object] | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        allow_empty_response: bool = False,
    ) -> dict[str, object]:
        if sum(value is not None for value in (form, json_body, raw_body)) > 1:
            raise ValueError("Provide only one request body format.")
        if raw_body is not None and not content_type:
            raise ValueError("A raw request body requires a content type.")
        body = raw_body if raw_body is not None else (urlencode(form).encode("utf-8") if form is not None else None)
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if raw_body is not None:
            request_headers["Content-Type"] = content_type or "application/octet-stream"
        elif form is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - Zoho URLs are fixed above.
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            raise ZohoCrmError(f"Zoho rejected the request ({ZohoCrmService._zoho_error_code(exc)}).") from exc
        except (TimeoutError, URLError) as exc:
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

    @staticmethod
    def _request_binary(url: str, *, headers: dict[str, str]) -> ZohoBinaryDownload:
        request = Request(url, headers={"Accept": "application/octet-stream, */*", **headers}, method="GET")
        try:
            with urlopen(request, timeout=_BINARY_DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 - Zoho URLs are fixed above.
                content = response.read(_MAX_EMAIL_ATTACHMENT_BYTES + 1)
                content_type = response.headers.get_content_type()
        except HTTPError as exc:
            raise ZohoCrmError(f"Zoho rejected the request ({ZohoCrmService._zoho_error_code(exc)}).") from exc
        except TimeoutError as exc:
            raise ZohoCrmError("Zoho hat beim Abruf des Anhangs nicht rechtzeitig geantwortet.") from exc
        except URLError as exc:
            raise ZohoCrmError("Zoho CRM is currently unreachable. Try again shortly.") from exc

        if len(content) > _MAX_EMAIL_ATTACHMENT_BYTES:
            raise ZohoCrmError("The Zoho email attachment exceeds the Hub download limit of 25 MB.")
        if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", content_type.casefold()):
            content_type = "application/octet-stream"
        return ZohoBinaryDownload(content=content, content_type=content_type)

    @staticmethod
    def _zoho_error_code(exc: HTTPError) -> str:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            if isinstance(error_payload, dict):
                provider_code = error_payload.get("code") or error_payload.get("error")
                if isinstance(provider_code, str):
                    return provider_code
        except Exception:
            pass
        return f"HTTP_{exc.code}"
