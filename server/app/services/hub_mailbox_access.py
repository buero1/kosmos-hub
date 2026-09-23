"""Record visibility shared by mailbox pages, drafts and agent queries."""

import json
import re

from sqlalchemy import select

from app.models.customer_communication import CustomerZohoEmail
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_documents import HubFinanceDunning
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_scheduled_email import HubScheduledEmail
from app.models.hub_user import HubUser
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_operations import HubOperationError
from app.services.hub_email_associations import EmailAssociationService
from app.services.hub_mailbox_permissions import MailboxPermissions


class HubMailboxAccess:
    def __init__(self, *, db, cipher, actor, account_id=None):
        from app.core.mailbox_actor import resolve_mailbox_actor
        actor = resolve_mailbox_actor(actor)
        self.db, self.cipher = db, cipher
        self.user = db.scalar(select(HubUser).where(HubUser.username == actor, HubUser.is_active.is_(True)))
        self.access = HubAccessControlService(db=db)
        self.mailboxes = MailboxPermissions(db=db, actor=actor, account_id=account_id)
        if self.user is None:
            raise HubOperationError("Fuer das Postfach fehlt die Berechtigung.")
        self.customers = self.access.accessible_record_ids(user=self.user, module_key="customers")
        self.leads = self.access.accessible_record_ids(user=self.user, module_key="leads")
        if not self.access.can(self.user, "customers", "view"):
            self.customers = set()
        if not self.access.can(self.user, "leads", "view"):
            self.leads = set()
        self.associations = EmailAssociationService(db=db, cipher=cipher)
        self._link_visibility = {}

    def record_visible(self, module, record_id):
        key = (module, record_id)
        if key not in self._link_visibility:
            if module == "customers":
                visible = self.customer_visible(record_id)
            elif module == "leads":
                visible = self.leads is None or record_id in self.leads
            elif module == "contacts":
                contact = self.db.get(CustomerContact, record_id)
                visible = contact is not None and self.access.can_access_contact(user=self.user, contact=contact)
            else:
                visible = False
            self._link_visibility[key] = visible
        return self._link_visibility[key]

    def customer_visible(self, customer_id):
        return customer_id is not None and (self.customers is None or customer_id in self.customers)

    def payload_visible(self, payload):
        for key, allowed in (("recipient_customer_id", self.customers), ("customer_id", self.customers), ("recipient_lead_id", self.leads), ("lead_id", self.leads)):
            value = payload.get(key)
            if value in (None, ""):
                continue
            if not str(value).isdecimal() or (allowed is not None and int(value) not in allowed):
                return False
        if payload.get("dunning_id"):
            raw = str(payload["dunning_id"])
            record = self.db.get(HubFinanceDunning, int(raw)) if raw.isdecimal() else None
            if record is None or not self.customer_visible(record.customer_id):
                return False
        return True

    def visible(self, record, action="view"):
        if not self.mailboxes.message_allowed(record, action):
            return False
        if isinstance(record, CustomerZohoEmail):
            return self.customer_visible(record.customer_id)
        try:
            payload = json.loads(self.cipher.decrypt(record.encrypted_payload_json))
        except (ValueError, TypeError):
            return False
        if not isinstance(payload, dict) or not self.payload_visible(payload):
            return False
        if isinstance(record, HubScheduledEmail):
            return ((record.customer_id is None or self.customer_visible(record.customer_id))
                    and (record.lead_id is None or self.leads is None or record.lead_id in self.leads))
        if isinstance(record, HubMailboxEmail) and record.mailbox_state != "draft":
            links = self.associations.links(payload, record.direction)
            return not links or any(self.record_visible(link.module, link.id) for link in links)
        return True

    def require(self, key, action="view"):
        record = None
        match = re.fullmatch(r"linked-([1-9]\d*)-([1-9]\d*)", key)
        if match:
            record = self.db.get(CustomerZohoEmail, int(match[2]))
            if record is not None and record.customer_id != int(match[1]):
                record = None
        else:
            match = re.fullmatch(r"(unassigned|scheduled)-([1-9]\d*)", key)
            if match:
                record = self.db.get(HubMailboxEmail if match[1] == "unassigned" else HubScheduledEmail, int(match[2]))
        if record is None or not self.visible(record, action):
            raise HubOperationError("Die E-Mail ist nicht verfuegbar.")
        return record
