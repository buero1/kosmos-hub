"""Shared address-based CRM associations for mailbox, record panels and agent reads."""
from dataclasses import dataclass
from email.utils import getaddresses
import json
from cryptography.fernet import InvalidToken

from sqlalchemy import delete, select

from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_email_address import HubEmailAddress
from app.models.hub_lead import HubLead
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_email import HubMailboxEmail


@dataclass(frozen=True)
class EmailRecordLink:
    module: str
    id: int
    name: str
    url: str


def addresses(value):
    if isinstance(value, dict):
        return addresses(value.get("email") or value.get("email_address") or value.get("address"))
    if isinstance(value, (list, tuple)):
        return set().union(*(addresses(item) for item in value)) if value else set()
    if not isinstance(value, str):
        return set()
    return {address.strip().casefold() for _, address in getaddresses([value.replace(";", ",")])
            if "@" in address and not any(char.isspace() for char in address)}


def counterpart_addresses(payload, direction):
    if direction == "inbound":
        return addresses(payload.get("from") or payload.get("absender") or payload.get("sender"))
    if direction == "outbound":
        return addresses([payload.get("to") or payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient"),
                          payload.get("cc"), payload.get("bcc")])
    return set()


def index_email(db, cipher, email):
    """Replace only the lookup index, in the same transaction as the stored message."""
    from app.services.hub_mailbox_permissions import bind_message
    bind_message(db, cipher, email)
    column = HubEmailAddress.customer_email_id if isinstance(email, CustomerZohoEmail) else HubEmailAddress.mailbox_email_id
    db.execute(delete(HubEmailAddress).where(column == email.id))
    if email.mailbox_state == "draft" or getattr(email, "sync_status", "sent") in {"pending", "failed"}:
        return
    payload = json.loads(cipher.decrypt(email.encrypted_payload_json))
    for address in counterpart_addresses(payload, email.direction):
        db.add(HubEmailAddress(**{column.key: email.id}, address_digest=cipher.search_digest("email-address", address)))


class EmailAssociationService:
    def __init__(self, *, db, cipher):
        self.db, self.cipher = db, cipher
        self._index = None

    def _fields(self, record):
        if not record.encrypted_profile_json:
            return {}
        try:
            value = json.loads(self.cipher.decrypt(record.encrypted_profile_json))
        except (ValueError, TypeError, InvalidToken):
            return {}
        fields = value.get("fields", value) if isinstance(value, dict) else {}
        return fields if isinstance(fields, dict) else {}

    def _records(self):
        if self._index is not None:
            return self._index
        index = {}
        own = set().union(*(addresses(value) for value in self.db.scalars(select(HubMailboxAccount.email_address))))
        customers = {row.id: row for row in self.db.scalars(select(Customer))}

        def add(link, fields, labels):
            for address in addresses([fields.get(label) for label in labels]) - own:
                index.setdefault(address, set()).add(link)

        labels = ("Kontakt-E-Mail", "E-Mail", "Zweite E-Mail-Adresse", "Dritte E-Mail-Adresse",
                  "email", "secondary_email", "third_email", "Email", "Secondary_Email")
        for row in customers.values():
            add(EmailRecordLink("customers", row.id, row.name, f"/customers/{row.id}"), self._fields(row), labels)
        for row in self.db.scalars(select(HubLead)):
            fields = self._fields(row)
            name = fields.get("company") or " ".join(str(fields.get(key) or "") for key in ("first_name", "last_name")).strip() or f"Lead {row.id}"
            add(EmailRecordLink("leads", row.id, str(name), f"/leads/{row.id}"), fields, labels)
        for row in self.db.scalars(select(CustomerContact)):
            fields = self._fields(row)
            name = fields.get("Name") or " ".join(str(fields.get(key) or "") for key in ("Vorname", "Nachname")).strip() or f"Kontakt {row.id}"
            url = f"/customers/{row.customer_id}/contacts/{row.id}" if row.customer_id else f"/contacts/{row.id}"
            add(EmailRecordLink("contacts", row.id, str(name), url), fields, labels)
            if row.customer_id in customers:
                customer = customers[row.customer_id]
                add(EmailRecordLink("customers", customer.id, customer.name, f"/customers/{customer.id}"), fields, labels)
        self._index = index
        return index

    def links(self, payload, direction):
        counterparts = counterpart_addresses(payload, direction)
        if not counterparts:
            return ()
        index = self._records()
        links = set().union(*(index.get(address, set()) for address in counterparts))
        return tuple(sorted(links, key=lambda link: (link.module, link.name.casefold(), link.id)))

    def records_for(self, module, record_id):
        """Current CRM addresses match historical messages too; no dangling entity links."""
        digests = {self.cipher.search_digest("email-address", address) for address, links in self._records().items()
                   if any(link.module == module and link.id == record_id for link in links)}
        if not digests:
            return ()
        rows = []
        for model, column in ((CustomerZohoEmail, HubEmailAddress.customer_email_id), (HubMailboxEmail, HubEmailAddress.mailbox_email_id)):
            statement = select(model).where(model.id.in_(select(column).where(HubEmailAddress.address_digest.in_(digests))), model.mailbox_state == "active")
            if model is CustomerZohoEmail:
                statement = statement.where(model.sync_status.not_in(("failed", "pending")))
            rows.extend(self.db.scalars(statement))
        return rows
