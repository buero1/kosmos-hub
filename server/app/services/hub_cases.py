"""Creation, validation and presentation of Hub-native cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_case import HubCase
from app.models.hub_case_email_link import HubCaseEmailLink
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_case_field_catalog import HUB_CASE_FIELDS, HubCaseField
from app.services.module_layouts import ModuleLayoutService


CASE_FIELDS_LAYOUT_KEY = "case-fields"


class HubCaseError(ValueError):
    """A safe validation message for the Fälle UI."""


@dataclass(frozen=True)
class HubCaseFieldValue:
    key: str
    label: str
    display_type: str
    value: str
    form_value: str
    required: bool = False
    read_only: bool = False
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class HubCaseListEntry:
    case: HubCase
    case_number: str
    status: str
    customer_name: str
    case_origin: str
    created_time: str


@dataclass(frozen=True)
class HubCaseDetail:
    case: HubCase
    case_number: str
    fields: tuple[HubCaseFieldValue, ...]
    status: str
    linked_emails: tuple["HubCaseLinkedEmail", ...]


@dataclass(frozen=True)
class HubCaseEmailSource:
    """A selected mailbox message that may be attached to a case."""

    key: str
    subject: str
    customer_id: int | None


@dataclass(frozen=True)
class HubCaseLinkedEmailAttachment:
    id: str
    filename: str
    download_url: str


@dataclass(frozen=True)
class HubCaseLinkedEmail:
    link_id: int
    source_key: str
    subject: str
    sender: str | None
    recipients: str | None
    direction: str
    is_unread: bool
    occurred_at: datetime | None
    preview_html: str | None
    attachments: tuple[HubCaseLinkedEmailAttachment, ...]
    can_load_content: bool
    last_error: str | None
    mailbox_folder: str


class HubCaseService:
    """Keep the reduced Fälle schema independent from Zoho CRM."""

    _BERLIN = ZoneInfo("Europe/Berlin")
    _MAX_LENGTHS = {
        "description": 20_000,
        "duration_minutes": 20,
        "billed_amount_net": 20,
        "status": 100,
        "case_reason": 255,
        "case_origin": 100,
    }

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_cases(self) -> tuple[HubCaseListEntry, ...]:
        cases = self.db.scalars(select(HubCase)).all()
        return self._list_entries(cases)

    def list_cases_for_customer(self, *, customer_id: int) -> tuple[HubCaseListEntry, ...]:
        """Return only the cases linked to one Hub customer for its detail view."""
        cases = self.db.scalars(
            select(HubCase)
            .where(HubCase.customer_id == customer_id)
        ).all()
        return self._list_entries(cases)

    def _list_entries(self, cases: list[HubCase]) -> tuple[HubCaseListEntry, ...]:
        entries: list[HubCaseListEntry] = []
        cases_with_values = [(case, self._values(case)) for case in cases]
        # The business creation time is encrypted with the case data, so order after decrypting it.
        cases_with_values.sort(
            key=lambda item: self._case_creation_sort_key(case=item[0], values=item[1]),
            reverse=True,
        )
        for case, values in cases_with_values:
            entries.append(
                HubCaseListEntry(
                    case=case,
                    case_number=self.case_number(case),
                    status=values.get("status") or "-",
                    customer_name=case.customer.name if case.customer is not None else "-",
                    case_origin=self._selection_display(values.get("case_origin", "")),
                    created_time=self._display_datetime(values.get("created_time", "")),
                )
            )
        return tuple(entries)

    @staticmethod
    def _case_creation_sort_key(*, case: HubCase, values: dict[str, str]) -> tuple[bool, str, str, int]:
        created_time = values.get("created_time", "")
        imported_at = case.created_at.isoformat() if case.created_at is not None else ""
        return (bool(created_time), created_time, imported_at, case.id)

    def list_linkable_customers(self) -> tuple[Customer, ...]:
        return tuple(
            self.db.scalars(
                select(Customer).where(Customer.is_visible.is_(True)).order_by(Customer.name.asc())
            ).all()
        )

    def get_detail(self, *, case_id: int) -> HubCaseDetail | None:
        case = self.db.scalar(
            select(HubCase)
            .options(
                selectinload(HubCase.email_links).selectinload(HubCaseEmailLink.customer_email),
                selectinload(HubCase.email_links).selectinload(HubCaseEmailLink.mailbox_email),
            )
            .where(HubCase.id == case_id)
        )
        if case is None:
            return None
        values = self._values(case)
        fields = tuple(self._field_value(definition, case=case, values=values) for definition in HUB_CASE_FIELDS)
        return HubCaseDetail(
            case=case,
            case_number=self.case_number(case),
            fields=self._field_display_layout(fields),
            status=values.get("status") or "-",
            linked_emails=self._linked_emails(case),
        )

    def new_form_values(self) -> dict[str, str]:
        return {
            "case_field__status": "Neu",
            "case_field__created_time": self._now_form_value(),
        }

    def create_case(self, *, customer_id: int | None, submitted_values: dict[str, str]) -> HubCase:
        customer = self._customer(customer_id)
        values = self._submitted_values(submitted_values, creating=True)
        if customer is not None:
            values["customer_name"] = customer.name
        case = HubCase(
            customer=customer,
            encrypted_fields_json=self._encrypt_values(values),
        )
        self.db.add(case)
        self.db.flush()
        case.case_number = f"FALL-{case.id:06d}"
        self.db.flush()
        return case

    def source_email(self, *, source_email_key: str) -> HubCaseEmailSource:
        """Resolve a mailbox key without guessing a relationship from email content."""
        customer_email, mailbox_email = self._source_email_record(source_email_key=source_email_key)
        if customer_email is not None:
            payload = self._payload(customer_email.encrypted_payload_json)
            return HubCaseEmailSource(
                key=f"linked-{customer_email.customer_id}-{customer_email.id}",
                subject=CustomerCommunicationService._text(payload.get("subject")) or "Ohne Betreff",
                customer_id=customer_email.customer_id,
            )
        assert mailbox_email is not None
        payload = self._payload(mailbox_email.encrypted_payload_json)
        return HubCaseEmailSource(
            key=f"unassigned-{mailbox_email.id}",
            subject=(
                CustomerCommunicationService._text(payload.get("betreff"))
                or CustomerCommunicationService._text(payload.get("subject"))
                or "Ohne Betreff"
            ),
            customer_id=None,
        )

    def link_email(self, *, case_id: int, source_email_key: str) -> HubCaseEmailLink:
        """Attach one deliberately selected source email to a case, idempotently."""
        case = self.db.get(HubCase, case_id)
        if case is None:
            raise HubCaseError("Der Fall wurde nicht gefunden.")
        customer_email, mailbox_email = self._source_email_record(source_email_key=source_email_key)
        if customer_email is not None:
            if case.customer_id is None:
                customer = self._customer(customer_email.customer_id)
                case.customer = customer
                values = self._values(case)
                values["customer_name"] = customer.name if customer is not None else ""
                case.encrypted_fields_json = self._encrypt_values(values)
            elif case.customer_id != customer_email.customer_id:
                raise HubCaseError("Diese Kunden-E-Mail kann nur einem Fall desselben Kunden zugeordnet werden.")
            existing = self.db.scalar(
                select(HubCaseEmailLink).where(
                    HubCaseEmailLink.case_id == case.id,
                    HubCaseEmailLink.customer_email_id == customer_email.id,
                )
            )
            if existing is not None:
                return existing
            link = HubCaseEmailLink(case=case, customer_email=customer_email)
        else:
            assert mailbox_email is not None
            existing = self.db.scalar(
                select(HubCaseEmailLink).where(
                    HubCaseEmailLink.case_id == case.id,
                    HubCaseEmailLink.mailbox_email_id == mailbox_email.id,
                )
            )
            if existing is not None:
                return existing
            link = HubCaseEmailLink(case=case, mailbox_email=mailbox_email)
        self.db.add(link)
        self.db.flush()
        return link

    def unlink_email(self, *, case_id: int, link_id: int) -> None:
        link = self.db.get(HubCaseEmailLink, link_id)
        if link is None or link.case_id != case_id:
            raise HubCaseError("Die E-Mail-Verknüpfung wurde nicht gefunden.")
        self.db.delete(link)
        self.db.flush()

    def update_case(self, *, case_id: int, customer_id: int | None, submitted_values: dict[str, str]) -> HubCase:
        case = self.db.get(HubCase, case_id)
        if case is None:
            raise HubCaseError("Der Fall wurde nicht gefunden.")
        case.customer = self._customer(customer_id)
        existing_values = self._values(case)
        values = self._submitted_values(
            submitted_values,
            creating=False,
            allowed_legacy_values=existing_values,
        )
        if case.customer is not None:
            values["customer_name"] = case.customer.name
        elif existing_values.get("customer_name"):
            values["customer_name"] = existing_values["customer_name"]
        case.encrypted_fields_json = self._encrypt_values(values)
        self.db.flush()
        return case

    def delete_case(self, *, case_id: int) -> HubCase:
        """Remove a case from the Hub without changing its Zoho source record."""
        case = self.db.get(HubCase, case_id)
        if case is None:
            raise HubCaseError("Der Fall wurde nicht gefunden.")
        self.db.delete(case)
        self.db.flush()
        return case

    @staticmethod
    def case_number(case: HubCase) -> str:
        return case.case_number or f"FALL-{case.id:06d}"

    def _customer(self, customer_id: int | None) -> Customer | None:
        if customer_id is None:
            return None
        customer = self.db.get(Customer, customer_id)
        if customer is None or not customer.is_visible:
            raise HubCaseError("Der ausgewählte Kunde ist nicht verfügbar.")
        return customer

    def _source_email_record(
        self,
        *,
        source_email_key: str,
    ) -> tuple[CustomerZohoEmail | None, HubMailboxEmail | None]:
        key = source_email_key.strip()
        if key.startswith("linked-"):
            try:
                customer_text, email_text = key.removeprefix("linked-").split("-", 1)
                customer_id = int(customer_text)
                email_id = int(email_text)
            except ValueError as exc:
                raise HubCaseError("Die ausgewählte E-Mail ist ungültig.") from exc
            email = self.db.scalar(
                select(CustomerZohoEmail).where(
                    CustomerZohoEmail.customer_id == customer_id,
                    CustomerZohoEmail.id == email_id,
                )
            )
            if email is None:
                raise HubCaseError("Die ausgewählte E-Mail wurde nicht gefunden.")
            return email, None
        if key.startswith("unassigned-"):
            try:
                email_id = int(key.removeprefix("unassigned-"))
            except ValueError as exc:
                raise HubCaseError("Die ausgewählte E-Mail ist ungültig.") from exc
            email = self.db.get(HubMailboxEmail, email_id)
            if email is None:
                raise HubCaseError("Die ausgewählte E-Mail wurde nicht gefunden.")
            return None, email
        raise HubCaseError("Diese E-Mail kann nicht mit einem Fall verknüpft werden.")

    def _linked_emails(self, case: HubCase) -> tuple[HubCaseLinkedEmail, ...]:
        views = [self._linked_email_view(link) for link in case.email_links]
        available_views = [view for view in views if view is not None]
        available_views.sort(key=lambda view: view.link_id, reverse=True)
        return tuple(available_views)

    def _linked_email_view(self, link: HubCaseEmailLink) -> HubCaseLinkedEmail | None:
        if link.customer_email is not None:
            email = link.customer_email
            payload = self._payload(email.encrypted_payload_json)
            content = CustomerCommunicationService._text(payload.get("content"))
            attachments = tuple(
                HubCaseLinkedEmailAttachment(
                    id=attachment.id,
                    filename=attachment.filename,
                    download_url=(
                        f"/customers/{email.customer_id}/communications/emails/{email.id}/attachments/{attachment.id}"
                    ),
                )
                for attachment in CustomerCommunicationService._email_attachments(payload)
            )
            return HubCaseLinkedEmail(
                link_id=link.id,
                source_key=f"linked-{email.customer_id}-{email.id}",
                subject=CustomerCommunicationService._text(payload.get("subject")) or "Ohne Betreff",
                sender=CustomerCommunicationService._people_text(payload.get("from")),
                recipients=CustomerCommunicationService._people_text(payload.get("to")),
                direction=email.direction,
                is_unread=email.is_unread,
                occurred_at=email.zoho_sent_at or email.created_at,
                preview_html=CustomerCommunicationService._email_preview_document(
                    content,
                    image_url_prefix=f"/customers/{email.customer_id}/communications/emails/{email.id}/images",
                ),
                attachments=attachments,
                can_load_content=content is None and bool(email.zoho_message_id and email.zoho_module and email.zoho_record_id),
                last_error=email.last_error,
                mailbox_folder=self._mailbox_folder(mailbox_state=email.mailbox_state, direction=email.direction),
            )
        if link.mailbox_email is not None:
            email = link.mailbox_email
            payload = self._payload(email.encrypted_payload_json)
            content = self._mailbox_content(payload)
            attachments = tuple(
                HubCaseLinkedEmailAttachment(
                    id=attachment.id,
                    filename=attachment.filename,
                    download_url=f"/emails/unassigned/{email.id}/attachments/{attachment.id}",
                )
                for attachment in CustomerCommunicationService._email_attachments(payload)
            )
            return HubCaseLinkedEmail(
                link_id=link.id,
                source_key=f"unassigned-{email.id}",
                subject=(
                    CustomerCommunicationService._text(payload.get("betreff"))
                    or CustomerCommunicationService._text(payload.get("subject"))
                    or "Ohne Betreff"
                ),
                sender=CustomerCommunicationService._people_text(
                    payload.get("absender") or payload.get("sender") or payload.get("from")
                ),
                recipients=CustomerCommunicationService._people_text(
                    payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient") or payload.get("to")
                ),
                direction=email.direction,
                is_unread=email.is_unread,
                occurred_at=email.received_at,
                preview_html=CustomerCommunicationService._email_preview_document(content),
                attachments=attachments,
                can_load_content=False,
                last_error=email.last_error,
                mailbox_folder=self._mailbox_folder(mailbox_state=email.mailbox_state, direction=email.direction),
            )
        return None

    def _payload(self, encrypted_payload_json: str) -> dict[str, object]:
        try:
            payload = self.cipher.decrypt(encrypted_payload_json)
            decoded = json.loads(payload)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _mailbox_content(payload: dict[str, object]) -> str | None:
        containers = [payload]
        for key in ("data", "payload", "record", "current_record", "currentrecord", "aufzeichnung"):
            value = payload.get(key)
            if isinstance(value, dict):
                containers.append(value)
            elif isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    containers.append(decoded)
        for container in containers:
            for key in ("content", "body", "message", "html", "nachricht", "email_content", "mail_content"):
                content = CustomerCommunicationService._text(container.get(key))
                if content:
                    return content
        return None

    @staticmethod
    def _mailbox_folder(*, mailbox_state: str, direction: str) -> str:
        if mailbox_state == "draft":
            return "drafts"
        if mailbox_state in {"trash", "spam"}:
            return mailbox_state
        return "inbox" if direction == "inbound" else "sent"

    def _submitted_values(
        self,
        submitted_values: dict[str, str],
        *,
        creating: bool,
        allowed_legacy_values: dict[str, str] | None = None,
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        for field in HUB_CASE_FIELDS:
            if field.key in {"case_number", "customer_name"}:
                continue
            value = str(submitted_values.get(f"case_field__{field.key}") or "").strip()
            if field.key == "created_time" and creating and not value:
                value = self._now_form_value()
            max_length = self._MAX_LENGTHS.get(field.key, 1_000)
            if len(value) > max_length:
                raise HubCaseError(f"{field.label} ist zu lang.")
            if field.required and (not value or value == "-None-"):
                raise HubCaseError(f"{field.label} ist erforderlich.")
            if (
                field.options
                and value
                and value not in field.options
                and value != (allowed_legacy_values or {}).get(field.key)
            ):
                raise HubCaseError(f"{field.label} enthält eine ungültige Auswahl.")
            if field.display_type == "Ganzzahl" and value:
                value = self._validated_integer(field, value)
            if field.display_type == "Datum und Uhrzeit" and value:
                value = self._validated_datetime(field, value)
            values[field.key] = value
        return values

    @staticmethod
    def _validated_integer(field: HubCaseField, value: str) -> str:
        try:
            number = int(value)
        except ValueError as exc:
            raise HubCaseError(f"{field.label} muss eine Ganzzahl sein.") from exc
        if number < 0:
            raise HubCaseError(f"{field.label} darf nicht negativ sein.")
        return str(number)

    @staticmethod
    def _validated_datetime(field: HubCaseField, value: str) -> str:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M").strftime("%Y-%m-%dT%H:%M")
        except ValueError as exc:
            raise HubCaseError(f"{field.label} enthält kein gültiges Datum mit Uhrzeit.") from exc

    def _field_value(
        self,
        field: HubCaseField,
        *,
        case: HubCase,
        values: dict[str, str],
    ) -> HubCaseFieldValue:
        if field.key == "case_number":
            raw_value = self.case_number(case)
            display_value = raw_value
        elif field.key == "customer_name":
            raw_value = str(case.customer_id or "")
            display_value = case.customer.name if case.customer is not None else values.get("customer_name", "")
        else:
            raw_value = values.get(field.key, "")
            display_value = raw_value
            if field.display_type == "Datum und Uhrzeit":
                display_value = self._display_datetime(raw_value)
            elif field.display_type == "Auswahlliste":
                display_value = self._selection_display(raw_value)
        return HubCaseFieldValue(
            key=field.key,
            label=field.label,
            display_type=field.display_type,
            value=display_value,
            form_value=raw_value,
            required=field.required,
            read_only=field.read_only,
            options=field.options,
        )

    def _field_display_layout(
        self,
        fields: tuple[HubCaseFieldValue, ...],
    ) -> tuple[HubCaseFieldValue, ...]:
        fields_by_key = {field.key: field for field in fields}
        ordered_keys = ModuleLayoutService(db=self.db).ordered_keys(
            layout_key=CASE_FIELDS_LAYOUT_KEY,
            default_keys=tuple(fields_by_key),
        )
        return tuple(fields_by_key[key] for key in ordered_keys)

    def _values(self, case: HubCase) -> dict[str, str]:
        try:
            raw_values = json.loads(self.cipher.decrypt(case.encrypted_fields_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(raw_values, dict):
            return {}
        return {str(key): str(value or "") for key, value in raw_values.items()}

    def _encrypt_values(self, values: dict[str, str]) -> str:
        return self.cipher.encrypt(json.dumps(values, ensure_ascii=False))

    def _now_form_value(self) -> str:
        return datetime.now(self._BERLIN).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")

    @staticmethod
    def _selection_display(value: str) -> str:
        return "" if value == "-None-" else value

    @staticmethod
    def _display_datetime(value: str) -> str:
        if not value:
            return ""
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M").strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return value
