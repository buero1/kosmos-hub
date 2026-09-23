"""Shared, permission-scoped mailbox readers and attachment loading."""
from urllib.parse import quote, unquote
from sqlalchemy import select
from app.core.config import get_settings

from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.services.hub_email_composition import mailbox_for
from app.services.hub_mailbox_transport import HubMailboxTransportService, DEFAULT_HUB_MAILBOX_SENDER_EMAIL
from app.services.hub_crm_readers import HubCrmReadService
from app.services.hub_cases import HubCaseService
from app.services.hub_spam_senders import HubSpamSenderService
from app.services.scheduled_emails import ScheduledEmailService
from app.services.hub_operations import HubOperationError, HubArtifact
from app.services.hub_record_access import require_actor, identifier


def mailbox_status_data(service, account_id=None):
    mailbox = mailbox_for(service, account_id)
    account_unread = mailbox.get_unread_count()
    return {"folder_counts": mailbox.get_folder_counts(), "account_unread_count": account_unread,
            "unread_count": mailbox_for(service).get_unread_count() if account_id is not None else account_unread}


def mailbox_accounts(service):
    require_actor(service, "emails", "view")
    from app.services.hub_mailbox_permissions import MailboxPermissions
    return MailboxPermissions(db=service.db, actor=service.actor).list_accounts()


def template_library(service):
    return mailbox_for(service).communications.list_email_templates()


def compose_options(service, account_id=None):
    require_actor(service, "emails", "view")
    senders = HubMailboxTransportService(db=service.db, cipher=service.cipher, actor=service.actor).list_senders()
    templates = template_library(service)
    from app.services.hub_mailbox_permissions import MailboxPermissions
    from app.models.hub_mailbox_account import HubMailboxAccount
    permissions = MailboxPermissions(db=service.db, actor=service.actor)
    if account_id is not None:
        MailboxPermissions(db=service.db, actor=service.actor, account_id=account_id)
        preferred = service.db.get(HubMailboxAccount, account_id).email_address
        senders = tuple(sorted(senders, key=lambda row: row.email != preferred))
    account_ids = {row.email_address: row.id for row in service.db.scalars(select(HubMailboxAccount))}
    return {"senders": [{"name": row.name, "email": row.email, "can_send": permissions.can(account_ids.get(row.email), "send")} for row in senders],
        "default_sender_email": senders[0].email if senders else "",
        "default_template_id": next((row.id for row in templates if row.name.casefold() == "standard_neu"), ""),
        "templates": [{key: getattr(row, key) for key in ("id", "name", "module", "category", "subject")} for row in templates],
        "sender_error": "" if senders else "Keine aktive lokale Absenderadresse eingerichtet."}


def recipients(service, *, query="", customer_id=None):
    user, access = require_actor(service, "emails", "view")
    allowed = access.accessible_record_ids(user=user, module_key="customers")
    if not access.can(user, "customers", "view"):
        allowed = set()
    contacts = {row.id for row in service.db.scalars(select(CustomerContact)) if access.can_access_contact(user=user, contact=row)}
    communications = mailbox_for(service).communications
    if customer_id is not None:
        customer = service.db.get(Customer, customer_id)
        if customer is None or not customer.is_visible or (allowed is not None and customer_id not in allowed):
            raise HubOperationError("Der Kunde ist nicht verfuegbar.")
        return [{"customer_id": customer.id, "customer_name": customer.name, "key": row.key, "name": row.name, "email": row.email}
            for row in communications.list_contact_recipients(customer_id=customer.id, allowed_contact_ids=contacts)]
    return [{"customer_id": row.customer_id, "customer_name": row.customer_name,
             "key": row.recipient.key, "name": row.recipient.name, "email": row.recipient.email}
        for row in communications.search_recipients(query=query, allowed_customer_ids=allowed, allowed_contact_ids=contacts)]


def scheduled_context(service, scheduled_id):
    mailbox = mailbox_for(service)
    mailbox.scope.require(f"scheduled-{scheduled_id}")
    return ScheduledEmailService(db=service.db, cipher=service.cipher, public_base_url=get_settings().public_base_url).get_compose_context(scheduled_email_id=scheduled_id)


def attachment_entries(service, email_key):
    mailbox = mailbox_for(service)
    row = mailbox.scope.require(email_key)
    if email_key.startswith("scheduled-"):
        attachments = ScheduledEmailService(db=service.db, cipher=service.cipher, public_base_url=get_settings().public_base_url).attachment_views(row)
        available = {str(item.id) for item in row.attachments}
        base = f"/emails/scheduled/{row.id}/attachments/"
    else:
        attachments = mailbox.communications._email_attachments(mailbox._payload(row.encrypted_payload_json))
        available = {item.source_attachment_id for item in row.stored_attachments}
        base = f"/customers/{row.customer_id}/communications/emails/{row.id}/attachments/" if email_key.startswith("linked-") else f"/emails/unassigned/{row.id}/attachments/"
    return [{"id": item.id, "filename": item.filename, "local_available": str(item.id) in available,
        "artifact_ref": f"email-attachment:{email_key}/{quote(str(item.id), safe='')}",
        "download_url": base + quote(str(item.id), safe="")} for item in attachments]


def download_attachment(service, email_key, attachment_id, *, allow_fetch=False):
    mailbox = mailbox_for(service)
    row = mailbox.scope.require(email_key)
    if email_key.startswith("scheduled-"):
        return ScheduledEmailService(db=service.db, cipher=service.cipher, public_base_url=get_settings().public_base_url).download_attachment(
            scheduled_email_id=row.id, attachment_id=identifier(str(attachment_id)))
    if email_key.startswith("linked-"):
        return mailbox.communications.download_email_attachment(customer_id=row.customer_id, email_id=row.id,
            attachment_id=str(attachment_id), allow_fetch=allow_fetch)
    return mailbox.download_unassigned_attachment(email_id=row.id, attachment_id=str(attachment_id))


def attachment_artifact(service, reference):
    key, separator, attachment_id = reference.partition("/")
    if not separator or not attachment_id:
        raise HubOperationError("Ungueltiger Anhangverweis.")
    result = download_attachment(service, key, unquote(attachment_id))
    return HubArtifact(filename=result.filename, content_type=result.content_type, content=result.content)


def mailbox_case_context(service, email_key, case_id=None):
    user, access = require_actor(service, "cases", "view")
    mailbox_for(service).scope.require(email_key)
    domain = HubCaseService(db=service.db, cipher=service.cipher)
    source = domain.source_email(source_email_key=email_key)
    linked = domain.linked_case_for_source_email(source_email_key=source.key)
    if case_id is None:
        if linked is not None:
            raise HubOperationError("Diese E-Mail ist bereits mit einem Fall verknuepft.")
        return source, None
    if linked is None or linked.case.id != case_id:
        raise HubOperationError("Der Fall ist nicht mehr mit dieser E-Mail verknuepft.")
    return source, HubCrmReadService(db=service.db, cipher=service.cipher, actor=service.actor).case_detail(case_id)


def spam_senders(service):
    user, _access = require_actor(service, "emails", "manage")
    if user.role != "admin":
        raise HubOperationError("Absendersperren duerfen nur Administratoren verwalten.")
    return HubSpamSenderService(db=service.db).list_senders()
