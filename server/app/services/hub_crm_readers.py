"""Read-only CRM projections shared by HTTP pages, templates and agent queries."""

from dataclasses import replace

from sqlalchemy import select

from app.models.customer_contact import CustomerContact
from app.models.hub_case import HubCase
from app.services.customer_directory import CustomerDirectoryService
from app.services.customer_activities import CustomerActivityService
from app.services.hub_cases import HubCaseService
from app.services.hub_mailbox_access import HubMailboxAccess
from app.services.hub_operation_activities import ACTIVITY_MODELS, _can_access
from app.services.hub_operations import HubOperationError
from app.services.hub_record_access import require_actor
from app.services.hub_activity_responsibility import ActivityResponsibility


class HubCrmReadService:
    def __init__(self, *, db, cipher, actor):
        self.db, self.cipher, self.actor = db, cipher, actor

    def contact_entries(self):
        user, access = require_actor(self, "contacts", "view")
        visible = {row.id for row in self.db.scalars(select(CustomerContact))
                   if access.can_access_contact(user=user, contact=row)}
        return CustomerDirectoryService(db=self.db, cipher=self.cipher).list_contact_entries(allowed_contact_ids=visible)

    def contact_detail(self, contact_id, *, customer_id=None):
        user, access = require_actor(self, "contacts", "view")
        row = self.db.get(CustomerContact, contact_id)
        if row is None or (customer_id is not None and row.customer_id != customer_id) or not access.can_access_contact(user=user, contact=row):
            raise HubOperationError("Der Kontakt ist nicht verfuegbar.")
        return CustomerDirectoryService(db=self.db, cipher=self.cipher).get_contact_detail_by_id(contact_id=row.id)

    def case_entries(self, *, query=""):
        user, access = require_actor(self, "cases", "view")
        domain = HubCaseService(db=self.db, cipher=self.cipher)
        visible = (entry for entry in domain.list_cases() if access.can_access_case(user=user, case=entry.case))
        return tuple(entry for entry in visible if not query or query.casefold() in
                     " ".join([entry.case_number, entry.customer_name, *domain._values(entry.case).values()]).casefold())

    def case_detail(self, case_id, *, customer_id=None):
        user, access = require_actor(self, "cases", "view")
        row = self.db.get(HubCase, case_id)
        if row is None or (customer_id is not None and row.customer_id != customer_id) or not access.can_access_case(user=user, case=row):
            raise HubOperationError("Der Fall ist nicht verfuegbar.")
        detail = HubCaseService(db=self.db, cipher=self.cipher).get_detail(case_id=row.id)
        scope = HubMailboxAccess(db=self.db, cipher=self.cipher, actor=self.actor)
        links = []
        if access.can(user, "emails", "view"):
            for link in detail.linked_emails:
                try:
                    scope.require(link.source_key)
                except ValueError:
                    continue
                links.append(link)
        return replace(detail, linked_emails=tuple(links))

    def activity_record(self, kind, activity_id):
        user, access = require_actor(self, "activities", "view")
        model = ACTIVITY_MODELS.get(kind)
        row = self.db.get(model, activity_id) if model else None
        if row is None or not _can_access(row, user, access, self.db):
            raise HubOperationError("Die Aktivitaet ist nicht verfuegbar.")
        return row

    def activity_entries(self, kind, *, view="all"):
        user, access = require_actor(self, "activities", "view")
        model = ACTIVITY_MODELS.get(kind)
        if model is None:
            raise HubOperationError("Die Aktivitaetsart ist ungueltig.")
        return ActivityResponsibility(self.db, user).filter_views(kind, CustomerActivityService(db=self.db).list_directory_entries(kind=kind), view)
