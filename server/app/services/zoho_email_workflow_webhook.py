"""Secure receiver for a manually configured Zoho CRM E-Mails webhook."""

from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest
from secrets import token_urlsafe

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.zoho_email_workflow_delivery import ZohoEmailWorkflowDelivery
from app.models.zoho_email_workflow_webhook import ZohoEmailWorkflowWebhook
from app.services.customer_communications import CustomerCommunicationService
from app.services.zoho_crm import ZohoCrmError


@dataclass(frozen=True)
class ZohoEmailWorkflowWebhookStatus:
    endpoint: str
    is_configured: bool
    last_received_at: datetime | None
    last_imported_at: datetime | None
    last_error: str | None


class ZohoEmailWorkflowWebhookService:
    """Validate incoming webhook calls and process their work outside Zoho's request timeout."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        public_base_url: str,
        communication_service: CustomerCommunicationService | None = None,
    ) -> None:
        self.db = db
        self.cipher = cipher
        self.public_base_url = public_base_url.rstrip("/")
        self.communication_service = communication_service or CustomerCommunicationService(
            db=db,
            cipher=cipher,
            public_base_url=public_base_url,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.public_base_url}/account/zoho/email-workflow-webhook/receive"

    def endpoint_for_token(self, token: str) -> str:
        return f"{self.endpoint}/{token}"

    def status(self) -> ZohoEmailWorkflowWebhookStatus:
        webhook = self._webhook()
        return ZohoEmailWorkflowWebhookStatus(
            endpoint=self.endpoint,
            is_configured=webhook is not None,
            last_received_at=webhook.last_received_at if webhook else None,
            last_imported_at=webhook.last_imported_at if webhook else None,
            last_error=webhook.last_error if webhook else None,
        )

    def rotate_token(self) -> str:
        webhook = self._webhook()
        token = token_urlsafe(32)
        if webhook is None:
            webhook = ZohoEmailWorkflowWebhook(encrypted_token=self.cipher.encrypt(token))
            self.db.add(webhook)
        else:
            webhook.encrypted_token = self.cipher.encrypt(token)
        webhook.last_error = None
        self.db.flush()
        return token

    def receive(self, *, token: str, payload: dict[str, object]) -> bool:
        webhook = self._webhook()
        if webhook is None or not token.strip():
            return False
        try:
            expected_token = self.cipher.decrypt(webhook.encrypted_token)
        except Exception:
            return False
        if not compare_digest(expected_token, token.strip()):
            return False

        now = datetime.now(UTC)
        webhook.last_received_at = now
        webhook.encrypted_last_payload_json = self.cipher.encrypt(
            json.dumps(payload, ensure_ascii=False, default=str)[:50_000]
        )
        self.db.add(
            ZohoEmailWorkflowDelivery(
                encrypted_payload_json=self.cipher.encrypt(
                    json.dumps(payload, ensure_ascii=False, default=str)[:50_000]
                ),
                received_at=now,
            )
        )
        webhook.last_error = None
        self.db.flush()
        return True

    def process_next_delivery(self) -> str | None:
        """Synchronize one queued delivery after Zoho has already received a response."""
        delivery = self.db.scalar(
            select(ZohoEmailWorkflowDelivery)
            .where(ZohoEmailWorkflowDelivery.status == "pending")
            .order_by(ZohoEmailWorkflowDelivery.id.asc())
            .limit(1)
        )
        if delivery is None:
            return None

        delivery_id = delivery.id
        started_at = datetime.now(UTC)
        delivery.status = "processing"
        delivery.started_at = started_at
        delivery.last_error = None
        self.db.commit()

        try:
            payload = json.loads(self.cipher.decrypt(delivery.encrypted_payload_json))
            if not isinstance(payload, dict):
                raise ValueError("Der gespeicherte Zoho-Webhook enthält kein JSON-Objekt.")
            customers = self._customers_for_payload(payload)
            if customers:
                for customer in customers:
                    self.communication_service.sync_customer(customer_id=customer.id, mark_new_emails_unread=True)
            else:
                self._store_unassigned_email(payload=payload, received_at=delivery.received_at)
        except (ValueError, ZohoCrmError, json.JSONDecodeError) as exc:
            self.db.rollback()
            self._finish_delivery(delivery_id=delivery_id, status="failed", error=str(exc))
            return "failed"
        except Exception as exc:
            self.db.rollback()
            self._finish_delivery(delivery_id=delivery_id, status="failed", error=str(exc))
            return "failed"
        else:
            self._finish_delivery(delivery_id=delivery_id, status="synced")
            return "succeeded"

    def _finish_delivery(self, *, delivery_id: int, status: str, error: str | None = None) -> None:
        delivery = self.db.get(ZohoEmailWorkflowDelivery, delivery_id)
        webhook = self._webhook()
        now = datetime.now(UTC)
        if delivery is not None:
            delivery.status = status
            delivery.completed_at = now
            delivery.last_error = error[:1000] if error else None
        if webhook is not None:
            if status == "synced":
                webhook.last_imported_at = now
                webhook.last_error = None
            else:
                webhook.last_error = error[:1000] if error else "Die Zoho-Webhook-Verarbeitung ist fehlgeschlagen."
        self.db.commit()

    def _webhook(self) -> ZohoEmailWorkflowWebhook | None:
        return self.db.scalar(select(ZohoEmailWorkflowWebhook).order_by(ZohoEmailWorkflowWebhook.id.asc()))

    def _customers_for_payload(self, payload: dict[str, object]) -> tuple[Customer, ...]:
        module = self._payload_value(
            payload,
            "record_module",
            "related_module",
            "parent_module",
            "related_to_module",
            "module",
        )
        record_id = self._payload_value(
            payload,
            "record_id",
            "related_record_id",
            "parent_id",
            "related_to_id",
            "entity_id",
        )
        if not module or not record_id:
            account_id = self._payload_value(payload, "account_id")
            contact_id = self._payload_value(payload, "contact_id")
            if account_id:
                module, record_id = "Accounts", account_id
            elif contact_id:
                module, record_id = "Contacts", contact_id
            else:
                return self._customers_for_email_addresses(payload)
        normalized_module = (module or "").strip().casefold()
        if normalized_module in {"account", "accounts"}:
            customer = self.db.scalar(select(Customer).where(Customer.zoho_id == record_id))
        elif normalized_module in {"contact", "contacts"}:
            contact = self.db.scalar(select(CustomerContact).where(CustomerContact.zoho_id == record_id))
            customer = contact.customer if contact is not None else None
        else:
            return self._customers_for_email_addresses(payload)
        if customer is None:
            return self._customers_for_email_addresses(payload)
        return (customer,)

    def _customers_for_email_addresses(self, payload: dict[str, object]) -> tuple[Customer, ...]:
        """Match either email endpoint to every linked customer contact."""
        addresses = self._email_addresses_from_payload(
            payload,
            "absender",
            "sender",
            "from",
            "from_address",
            "empfaenger",
            "empfänger",
            "recipient",
            "to",
            "sent_to",
        )
        if not addresses:
            return ()

        matched_customer_ids: set[int] = set()
        for contact in self.db.scalars(select(CustomerContact)).all():
            if addresses & self._profile_email_addresses(contact.encrypted_profile_json):
                matched_customer_ids.add(contact.customer_id)

        customers = tuple(
            customer
            for customer_id in sorted(matched_customer_ids)
            if (customer := self.db.get(Customer, customer_id)) is not None
        )
        if customers:
            return customers
        return ()

    def _store_unassigned_email(self, *, payload: dict[str, object], received_at: datetime) -> None:
        normalized_payload = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
        fingerprint = hashlib.sha256(normalized_payload.encode("utf-8")).hexdigest()
        latest = self.db.scalar(
            select(HubMailboxEmail)
            .where(HubMailboxEmail.fingerprint == fingerprint)
            .order_by(HubMailboxEmail.received_at.desc())
            .limit(1)
        )
        if latest is not None:
            latest_received_at = latest.received_at
            if latest_received_at.tzinfo is None:
                latest_received_at = latest_received_at.replace(tzinfo=UTC)
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
            if (received_at - latest_received_at).total_seconds() < 300:
                return
        direction = self._workflow_email_direction(payload)
        self.db.add(
            HubMailboxEmail(
                direction=direction,
                is_unread=direction == "inbound",
                fingerprint=fingerprint,
                encrypted_payload_json=self.cipher.encrypt(normalized_payload),
                received_at=received_at,
            )
        )

    @classmethod
    def _workflow_email_direction(cls, payload: dict[str, object]) -> str:
        direction = cls._payload_value(payload, "direction", "richtung")
        if direction and direction.casefold() in {"inbound", "outbound"}:
            return direction.casefold()
        if payload.get("sent") is True:
            return "outbound"
        return "inbound"

    @classmethod
    def _payload_value(cls, payload: dict[str, object], *names: str) -> str | None:
        wanted = {cls._normalized_key(name) for name in names}
        containers: list[dict[str, object]] = [payload]
        for key in ("data", "payload"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
        for container in containers:
            for key, value in container.items():
                if cls._normalized_key(key) not in wanted:
                    continue
                if isinstance(value, dict):
                    value = value.get("id") or value.get("value")
                if isinstance(value, (str, int, float)):
                    normalized = str(value).strip()
                    if normalized:
                        return normalized
        return None

    @classmethod
    def _email_addresses_from_payload(cls, payload: dict[str, object], *names: str) -> set[str]:
        wanted = {cls._normalized_key(name) for name in names}
        addresses: set[str] = set()
        containers: list[dict[str, object]] = [payload]
        for key in ("data", "payload"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
        for container in containers:
            for key, value in container.items():
                if cls._normalized_key(key) in wanted:
                    addresses.update(cls._email_addresses_from_value(value))
        return addresses

    @staticmethod
    def _email_addresses_from_value(value: object) -> set[str]:
        if isinstance(value, dict):
            values = value.values()
        elif isinstance(value, (list, tuple, set)):
            values = value
        else:
            values = (value,)
        addresses: set[str] = set()
        for candidate in values:
            if isinstance(candidate, (dict, list, tuple, set)):
                addresses.update(ZohoEmailWorkflowWebhookService._email_addresses_from_value(candidate))
            elif isinstance(candidate, str):
                addresses.update(match.casefold() for match in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", candidate, flags=re.IGNORECASE))
        return addresses

    def _profile_email_addresses(self, encrypted_profile_json: str | None) -> set[str]:
        if not encrypted_profile_json:
            return set()
        try:
            profile_json = self.cipher.decrypt(encrypted_profile_json)
        except Exception:
            return set()
        return self._email_addresses_from_value(profile_json)

    @staticmethod
    def _normalized_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())
