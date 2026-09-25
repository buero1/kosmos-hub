"""Transactional, one-time Lead conversion; never send or transfer email."""
from datetime import UTC, datetime
import json

from sqlalchemy import func, select

from app.core.record_actor import resolve_record_actor
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerZohoNote
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.models.hub_access_control import HubRecordAccessGrant, HubRecordAssignment
from app.models.hub_lead_conversion import HubLeadConversion
from app.models.hub_lead_note import HubLeadNote
from app.models.hub_record_info import HubRecordInfo
from app.models.hub_user import HubUser
from app.models.hub_workflow import HubWorkflow
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_activity_responsibility import ACTIVITY_MODELS
from app.services.hub_email_associations import addresses
from app.services.hub_record_info import record_info
from app.services.hub_workflows import LEAD_CUSTOMER_CONVERSION_WORKFLOW_KEY, ORDER_LEAD_RESULTS
from app.services.zoho_crm import ZohoCrmService


class LeadConversionService:
    def __init__(self, *, db, cipher):
        self.db, self.cipher = db, cipher
        self.directory = CustomerDirectoryService(db=db, cipher=cipher)

    def on_change(self, *, lead, previous, values):
        if values.get("lead_result") not in ORDER_LEAD_RESULTS or values.get("lead_result") == previous.get("lead_result"):
            return
        workflow = self.db.scalar(select(HubWorkflow).where(HubWorkflow.workflow_key == LEAD_CUSTOMER_CONVERSION_WORKFLOW_KEY))
        if workflow is None or not workflow.is_enabled:
            return
        if self.db.scalar(select(HubLeadConversion.id).where(HubLeadConversion.lead_id == lead.id).with_for_update()) is not None:
            return
        actor = resolve_record_actor(self.db)
        user = self.db.get(HubUser, actor.user_id) if actor.user_id else None
        access = HubAccessControlService(db=self.db)
        if user is not None and (not user.is_active or any(
            not access.can(user, module, action)
            for module in ("customers", "contacts") for action in ("view", "create")
        )):
            raise ValueError("Für die Umwandlung werden Lesen und Anlegen bei Kunden und Kontakten benötigt.")
        company = str(values.get("company") or "").strip()
        if not company:
            raise ValueError("Für die Umwandlung bitte die Firma im Lead ergänzen.")
        customer_values = {key: str(values.get(source) or "").strip() for key, source in {
            "customer_name": "company", "phone": "phone", "website": "website", "industry": "industry",
            "billing_street": "street", "billing_postal_code": "postal_code", "billing_city": "city",
            "billing_country": "country", "order_date": "order_date", "source": "source",
        }.items()}
        customer_values.update(account_status="Neu", customer_type="Kunde")
        contact_values = {key: str(values.get(source) or "").strip() for key, source in {
            "salutation": "salutation", "letter_salutation": "letter_salutation", "first_name": "first_name",
            "last_name": "last_name", "function": "position", "email": "email", "secondary_email": "secondary_email",
            "phone": "phone", "alternate_phone": "phone_secondary", "mobile": "mobile",
            "mailing_street": "street", "mailing_postal_code": "postal_code", "mailing_city": "city",
            "mailing_country": "country",
        }.items()}
        if contact_values["salutation"] in {"Frau Dr.", "Herr Dr."}:
            contact_values["salutation"] = contact_values["salutation"].split()[0]
            contact_values["title"] = "Dr."
        contact_values["customer_status"] = "Neu"
        customer_input = {"customer_field__" + key: value for key, value in customer_values.items()}
        contact_input = {"contact_field__" + key: value for key, value in contact_values.items()}
        self.directory._hub_customer_values(customer_input)
        self.directory._hub_contact_values(contact_input, require_salutation=True)
        self._check_duplicates(company, customer_values["website"], contact_values)

        customer = self.directory.create_hub_customer(submitted_values=customer_input)
        contact = self.directory.create_hub_contact(customer_id=customer.id, submitted_values=contact_input)
        conversion = HubLeadConversion(lead_id=lead.id, customer_id=customer.id, contact_id=contact.id,
            converted_at=datetime.now(UTC), actor_user_id=actor.user_id, actor_name=actor.name, actor_origin=actor.origin)
        self.db.add(conversion)
        self.db.flush()
        self._copy_access(lead.id, customer.id, user)
        self._copy_notes(lead.id, customer.id)
        self._move_activities(lead.id, customer)
        self.db.flush()
        return conversion

    def _check_duplicates(self, company, website, contact_values):
        domain = ZohoCrmService.normalize_website_domain(website)
        condition = func.lower(func.trim(Customer.name)) == company.lower()
        if domain:
            condition |= Customer.website_domain == domain
        if self.db.scalar(select(Customer.id).where(condition).limit(1)) is not None:
            raise ValueError("Mögliche Kundendublette: Firma oder Website ist bereits vorhanden. Bitte vor der Umwandlung prüfen.")
        emails = addresses([contact_values["email"], contact_values["secondary_email"]])
        if emails:
            for contact in self.db.scalars(select(CustomerContact)):
                fields = json.loads(self.cipher.decrypt(contact.encrypted_profile_json)).get("fields", {})
                if emails & addresses([fields.get(key) for key in ("E-Mail", "Zweite E-Mail-Adresse", "Dritte E-Mail-Adresse")]):
                    raise ValueError("Mögliche Kontaktdublette: Eine E-Mail-Adresse ist bereits einem Kontakt zugeordnet. Bitte vor der Umwandlung prüfen.")

    def _copy_access(self, lead_id, customer_id, user):
        access = HubAccessControlService(db=self.db)
        assignment = self.db.scalar(select(HubRecordAssignment).where(
            HubRecordAssignment.module_key == "leads", HubRecordAssignment.record_id == lead_id))
        grants = list(self.db.scalars(select(HubRecordAccessGrant).where(
            HubRecordAccessGrant.module_key == "leads", HubRecordAccessGrant.record_id == lead_id)))
        # Contacts inherit customer access; they have no independent record assignments.
        if assignment:
            access.assign_record(module_key="customers", record_id=customer_id,
                                 owner_user_id=assignment.owner_user_id, team_id=assignment.team_id)
        elif user:
            access.assign_created_record(user=user, module_key="customers", record_id=customer_id)
        for grant in grants:
            access.add_grant(module_key="customers", record_id=customer_id, user_id=grant.user_id,
                             team_id=grant.team_id, can_edit=grant.can_edit)
        if user and not access.can_access_record(user=user, module_key="customers", record_id=customer_id):
            access.add_grant(module_key="customers", record_id=customer_id, user_id=user.id, team_id=None, can_edit=True)

    def _copy_notes(self, lead_id, customer_id):
        for original in self.db.scalars(select(HubLeadNote).where(HubLeadNote.lead_id == lead_id)):
            info = record_info(original)
            copied = CustomerZohoNote(customer_id=customer_id, source="hub", sync_status="local",
                encrypted_payload_json=original.encrypted_payload_json, created_by_username=original.created_by_username,
                created_at=original.created_at, updated_at=original.updated_at,
                zoho_created_at=original.zoho_created_at, zoho_modified_at=original.zoho_modified_at)
            self.db.add(copied)
            self.db.flush()
            metadata = self.db.get(HubRecordInfo, ("customer_zoho_notes", copied.id))
            if metadata is None:
                metadata = HubRecordInfo(record_table="customer_zoho_notes", record_id=copied.id)
                self.db.add(metadata)
            old = self.db.get(HubRecordInfo, ("hub_lead_notes", original.id))
            for prefix in ("created", "changed"):
                setattr(metadata, prefix + "_at", info[prefix + "_at"])
                setattr(metadata, prefix + "_name", getattr(old, prefix + "_name", None) or
                        (original.created_by_username if prefix == "created" else None))
                setattr(metadata, prefix + "_user_id", getattr(old, prefix + "_user_id", None))
                setattr(metadata, prefix + "_origin", getattr(old, prefix + "_origin", None))
            self.db.flush()

    def _move_activities(self, lead_id, customer):
        access = HubAccessControlService(db=self.db)
        for kind, model in ACTIVITY_MODELS.items():
            for activity in self.db.scalars(select(model).where(model.lead_id == lead_id).with_for_update()):
                condition = (CustomerTaskEmailReminder.activity_kind == kind) & (CustomerTaskEmailReminder.activity_id == activity.id)
                if kind == "task":
                    condition |= CustomerTaskEmailReminder.task_id == activity.id
                reminders = list(self.db.scalars(select(CustomerTaskEmailReminder).where(condition).with_for_update()
                                               .execution_options(populate_existing=True)))
                if any(reminder.status == "sending" for reminder in reminders):
                    raise ValueError("Eine Erinnerung wird gerade versendet. Bitte kurz warten und die Umwandlung erneut speichern.")
                assignee = self.db.get(HubUser, activity.assignee_user_id) if activity.assignee_user_id else None
                if (assignee and assignee.is_active and access.can(assignee, "customers", "view")
                    and access.can_access_record(user=assignee, module_key="leads", record_id=lead_id)
                    and not access.can_access_record(user=assignee, module_key="customers", record_id=customer.id)):
                    access.add_grant(module_key="customers", record_id=customer.id, user_id=assignee.id, team_id=None,
                        can_edit=access.can_access_record(user=assignee, module_key="leads", record_id=lead_id, action="edit"))
                activity.customer_id, activity.lead_id = customer.id, None
                for reminder in reminders:
                    if reminder.status not in {"scheduled", "retrying"}:
                        continue
                    reminder.customer_id, reminder.customer_name = customer.id, customer.name
                for notification in self.db.scalars(select(CustomerActivityReminderNotification).where(
                    CustomerActivityReminderNotification.activity_kind == kind,
                    CustomerActivityReminderNotification.activity_id == activity.id).with_for_update()):
                    notification.customer_id = customer.id
