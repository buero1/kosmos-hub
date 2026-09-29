import json
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerEmailAttachment, CustomerZohoEmail, CustomerZohoEmailImage, CustomerZohoNote
from app.models.customer_contact import CustomerContact
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.zoho_email_template import ZohoEmailTemplate
from app.services.customer_communications import CustomerCommunicationAttachmentUpload, CustomerCommunicationService
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.email_composer_settings import EmailComposerSettingsService
from app.services.hub_mailbox_transport import HubMailboxTransportDelivery
from app.services.hub_record_catalog import RecordDataError as ZohoCrmError


class FakeZohoCommunications:
    def __init__(self):
        self.created_notes: list[tuple[str, str, str]] = []
        self.updated_notes: list[tuple[str, str, str]] = []
        self.deleted_notes: list[str] = []
        self.sent_emails: list[tuple[str, str, str, str, str, str, str, str | None]] = []
        self.sent_cc_recipients: list[tuple[tuple[str, str], ...]] = []
        self.sent_attachment_ids: list[tuple[str, ...]] = []
        self.reply_to_calls: list[tuple[str | None, str | None]] = []
        self.template_detail_requests: list[str] = []
        self.downloaded_attachments: list[tuple[str, str, str, str, str, str]] = []
        self.downloaded_inline_images: list[tuple[str, str, str, str, str]] = []
        self.email_content_requests: list[str | None] = []
        self.uploaded_files: list[tuple[str, bytes, str]] = []

    def list_account_notes(self, account_id: str) -> list[dict[str, object]]:
        assert account_id == "zoho-account-1"
        return [
            {
                "id": "zoho-note-1",
                "Note_Title": "Zoho note",
                "Note_Content": "Imported note content",
                "Created_Time": "2026-09-02T08:00:00+00:00",
                "Modified_Time": "2026-09-02T08:10:00+00:00",
                "Created_By": {"name": "Zoho Operator"},
            }
        ]
    def list_record_email_headers(self, module: str, record_id: str) -> list[dict[str, object]]:
        if module == "Accounts":
            assert record_id == "zoho-account-1"
            return [
                {
                    "id": "zoho-email-account-1",
                    "subject": "Anfrage",
                    "owner": {"id": "zoho-owner-1"},
                    "from": {"name": "Client", "email": "client@example.de"},
                    "to": [{"name": "Team", "email": "team@example.de"}],
                    "direction": "inbound",
                    "received_time": "2026-09-02T08:20:00+00:00",
                    "attachments": [{"id": "zoho-attachment-1", "name": "Angebot.pdf"}],
                }
            ]
        assert module == "Contacts"
        assert record_id == "zoho-contact-1"
        return []

    def get_record_email(
        self,
        *,
        module: str,
        record_id: str,
        message_id: str,
        user_id: str | None = None,
    ) -> dict[str, object]:
        assert (module, record_id, message_id) == ("Accounts", "zoho-account-1", "zoho-email-account-1")
        self.email_content_requests.append(user_id)
        return {"id": message_id, "content": "Full imported email body"}

    def download_record_email_attachment(
        self,
        *,
        module: str,
        record_id: str,
        message_id: str,
        user_id: str,
        attachment_id: str,
        filename: str,
    ):
        self.downloaded_attachments.append((module, record_id, message_id, user_id, attachment_id, filename))
        return type("DownloadedAttachment", (), {"content": b"%PDF-test", "content_type": "application/pdf"})()

    def download_record_email_inline_image(
        self,
        *,
        module: str,
        record_id: str,
        message_id: str,
        user_id: str,
        image_id: str,
    ):
        self.downloaded_inline_images.append((module, record_id, message_id, user_id, image_id))
        return type("DownloadedInlineImage", (), {"content": b"inline-image-bytes", "content_type": "image/png"})()

    def list_allowed_from_addresses(self) -> list[dict[str, object]]:
        return [{"user_name": "Hub Team", "email": "team@example.de"}]

    def list_email_templates(self) -> list[dict[str, object]]:
        return [
            {"id": "zoho-template-1", "name": "Statusvorlage", "subject": "Aktueller Stand", "module": {"api_name": "Accounts"}},
            {"id": "zoho-template-2", "name": "Kontaktvorlage", "subject": "Kontakt", "module": {"api_name": "Contacts"}},
        ]

    def get_email_template(self, *, template_id: str) -> dict[str, object]:
        self.template_detail_requests.append(template_id)
        if template_id == "zoho-template-2":
            return {
                "id": template_id,
                "name": "Kontaktvorlage",
                "subject": "Kontakt",
                "content": "<p>Hallo ${Accounts.Account_Name}</p>",
                "module": {"api_name": "Contacts"},
                "folder": {"id": "folder-1", "name": "Kunden"},
                "category": "normal",
            }
        assert template_id == "zoho-template-1"
        return {
            "id": template_id,
            "name": "Statusvorlage",
            "subject": "Aktueller Stand für ${Accounts.Account_Name}",
            "content": '<table style="width: 100%"><tr><td><strong>Hallo ${Accounts.Account_Name} ${!Accounts.id} ${!Accounts.Dialfire_WV_Datum} ${!Accounts.Dialfire_WV_Notiz}</strong></td></tr></table>',
            "module": {"api_name": "Accounts"},
            "folder": {"id": "folder-1", "name": "Kunden"},
            "category": "normal",
        }

    def create_account_note(self, *, account_id: str, title: str, content: str) -> dict[str, object]:
        self.created_notes.append((account_id, title, content))
        return {"id": "zoho-note-hub-1"}

    def update_note(self, *, note_id: str, title: str, content: str) -> dict[str, object]:
        self.updated_notes.append((note_id, title, content))
        return {"details": {"id": note_id}}

    def delete_note(self, *, note_id: str) -> None:
        self.deleted_notes.append(note_id)

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
        self.sent_emails.append((account_id, sender_name, sender_email, recipient_name, recipient_email, subject, content, template_id))
        self.sent_cc_recipients.append(cc_recipients)
        self.sent_attachment_ids.append(attachment_ids)
        self.reply_to_calls.append((reply_to_message_id, reply_to_owner_id))
        return {"message_id": "zoho-email-hub-1"}

    def upload_file_to_zfs(self, *, filename: str, content: bytes, content_type: str) -> str:
        self.uploaded_files.append((filename, content, content_type))
        return f"zfs-{len(self.uploaded_files)}"


def test_note_title_stays_empty_when_omitted():
    from app.services.hub_note_catalog import normalize_note

    values = normalize_note(title="", content="\n  Anfrage zur Rechnung\nWeitere Details", creating=True)
    assert values["title"] == ""
    assert values["content"] == "Anfrage zur Rechnung\nWeitere Details"


def _service(
    db: Session,
    fake_zoho: FakeZohoCommunications,
    attachment_storage: EmailAttachmentStorage | None = None,
) -> CustomerCommunicationService:
    return CustomerCommunicationService(
        db=db,
        cipher=SecretCipher("a" * 32),
        public_base_url="https://hub.example",
        attachment_storage=attachment_storage,
    )


def _seed_templates(service):
    fixture = FakeZohoCommunications()
    for summary in fixture.list_email_templates():
        if service.db.scalar(select(ZohoEmailTemplate).where(ZohoEmailTemplate.zoho_template_id == summary['id'])):
            continue
        payload = fixture.get_email_template(template_id=summary['id'])
        payload['folder_name'] = payload['folder']['name']
        service.db.add(ZohoEmailTemplate(
            zoho_template_id=summary['id'], module=payload['module']['api_name'],
            encrypted_payload_json=service.cipher.encrypt(json.dumps(payload)),
            is_active=True, zoho_synced_at=datetime.now(UTC),
        ))
    service.db.flush()


def _customer(cipher: SecretCipher) -> tuple[Customer, CustomerContact]:
    customer = Customer(
        name="Example Customer",
        zoho_id="zoho-account-1",
        encrypted_profile_json=cipher.encrypt(
            json.dumps(
                {
                    "fields": {
                        "Kontakt-E-Mail": "accounts@example.de",
                        "Arbeitsdomain": "https://work.example-customer.de",
                        "Update-Datum": "2026-09-02",
                        "Update-Notiz": "Bitte nachfassen",
                        "Webseite": "https://example-customer.de",
                    }
                }
            )
        ),
    )
    contact = CustomerContact(
        customer=customer,
        zoho_id="zoho-contact-1",
        encrypted_profile_json=cipher.encrypt(
            json.dumps(
                {
                    "fields": {
                        "Name": "Anna Example",
                        "Anrede": "Frau",
                        "Briefanrede": "Sehr geehrte Frau",
                        "Vorname": "Anna",
                        "Nachname": "Example",
                        "E-Mail": "anna@example.de",
                        "Zweite E-Mail-Adresse": "anna.private@example.de",
                    }
                }
            )
        ),
    )
    return customer, contact


def test_customer_communications_lists_recipients_without_loading_the_full_view():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        recipients = _service(db, FakeZohoCommunications()).list_recipients(customer_id=customer.id)

        assert [(recipient.name, recipient.email) for recipient in recipients] == [
            ("Example Customer", "accounts@example.de"),
            ("Anna Example", "anna@example.de"),
            ("Anna Example", "anna.private@example.de"),
        ]


def test_customer_communications_lists_each_linked_contact_email_for_a_new_customer_email():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        recipients = _service(db, FakeZohoCommunications()).list_contact_recipients(customer_id=customer.id)

        assert [(recipient.name, recipient.email) for recipient in recipients] == [
            ("Anna Example", "anna@example.de"),
            ("Anna Example", "anna.private@example.de"),
        ]


def test_customer_communications_searches_known_recipients_across_customers():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        other_customer = Customer(
            name="Other Customer",
            zoho_id="zoho-account-2",
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Kontakt-E-Mail": "other@example.de"}})),
        )
        db.add_all([customer, contact, other_customer])
        db.commit()

        matches = _service(db, FakeZohoCommunications()).search_recipients(query="anna@example")

        assert [
            (match.customer_id, match.customer_name, match.recipient.name, match.recipient.email)
            for match in matches
        ] == [(customer.id, "Example Customer", "Anna Example", "anna@example.de")]


def test_customer_communications_searches_hidden_test_customers_by_customer_type():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        test_customer = Customer(
            name="Test Customer",
            zoho_id="zoho-account-test",
            is_visible=False,
            encrypted_profile_json=cipher.encrypt(
                json.dumps({"fields": {"Kunde Typ": "Analyst", "Kontakt-E-Mail": "test@example.de"}})
            ),
        )
        test_contact = CustomerContact(
            customer=test_customer,
            zoho_id="zoho-contact-test",
            encrypted_profile_json=cipher.encrypt(
                json.dumps({"fields": {"Name": "Test Contact", "E-Mail": "test.contact@example.de"}})
            ),
        )
        hidden_customer = Customer(
            name="Hidden Customer",
            zoho_id="zoho-account-hidden",
            is_visible=False,
            encrypted_profile_json=cipher.encrypt(
                json.dumps({"fields": {"Kunde Typ": "Interessent", "Kontakt-E-Mail": "hidden@example.de"}})
            ),
        )
        db.add_all([test_customer, test_contact, hidden_customer])
        db.commit()

        matches = _service(db, FakeZohoCommunications()).search_recipients(query="@example.de")

        assert {(match.customer_name, match.recipient.name, match.recipient.email) for match in matches} == {
            ("Test Customer", "Test Customer", "test@example.de"),
            ("Test Customer", "Test Contact", "test.contact@example.de"),
        }


def test_customer_communications_marks_all_shared_email_copies_read():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        first_customer, _first_contact = _customer(cipher)
        second_customer = Customer(name="Second customer", zoho_id="zoho-account-2")
        first_email = CustomerZohoEmail(
            customer=first_customer,
            zoho_message_id="zoho-shared-email-1",
            source="zoho",
            direction="inbound",
            is_unread=True,
            sync_status="synced",
            encrypted_payload_json=cipher.encrypt('{"subject":"Shared message"}'),
        )
        second_email = CustomerZohoEmail(
            customer=second_customer,
            zoho_message_id="zoho-shared-email-1",
            source="zoho",
            direction="inbound",
            is_unread=True,
            sync_status="synced",
            encrypted_payload_json=cipher.encrypt('{"subject":"Shared message"}'),
        )
        db.add_all([first_customer, second_customer, first_email, second_email])
        db.commit()

        _service(db, FakeZohoCommunications()).mark_email_read(
            customer_id=first_customer.id,
            email_id=first_email.id,
        )

        assert first_email.is_unread is False
        assert second_email.is_unread is False


def test_customer_communications_retains_historical_headers_without_remote_loading():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        imported = CustomerZohoEmail(customer_id=customer.id, source="zoho", direction="inbound",
            is_unread=True, sync_status="synced", zoho_message_id="legacy-id",
            encrypted_payload_json=cipher.encrypt('{"subject":"Retained header"}'))
        db.add(imported); db.flush()
        before = imported.encrypted_payload_json
        with pytest.raises(ValueError, match="historischen E-Mail"):
            service._load_email_content_for_email(imported, mark_as_read=False)
        assert imported.is_unread is True
        assert imported.encrypted_payload_json == before
        assert fake_zoho.email_content_requests == []


def test_customer_communications_treats_zoho_unsent_headers_as_inbound():
    assert CustomerCommunicationService._email_direction({"sent": False}) == "inbound"


def _authorize_operator(db, monkeypatch, *, mittwald=False):
    from app.models.hub_user import HubUser
    from app.services.hub_mailbox_transport import HubMailboxTransportService
    from mailbox_fixture_helpers import mailbox_account
    db.add(HubUser(username="operator", password_hash="x", role="admin"))
    mailbox_account(db, SecretCipher("a" * 32), "team@example.de").display_name = "Hub Team"
    db.flush()
    if not mittwald:
        monkeypatch.setattr(HubMailboxTransportService, "is_configured", lambda self: False)


def test_customer_communications_compiles_hub_email_before_mittwald_delivery(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([
            customer,
            contact,
            HubMailboxAccount(
                email_address="team@example.de",
                display_name="Hub Team",
                username="team@example.de",
                encrypted_password=cipher.encrypt("mittwald-secret"),
                verified_at=datetime.now(UTC),
            ),
        ])
        db.commit()
        delivered_html: list[str] = []

        def fake_send(_self, **kwargs):
            delivered_html.append(kwargs["html_content"])
            return HubMailboxTransportDelivery(
                message_id="<mittwald-message@example.de>",
                sent_at=datetime.now(UTC),
            )

        monkeypatch.setattr("app.services.customer_communications.HubMailboxTransportService.send", fake_send)
        _authorize_operator(db, monkeypatch, mittwald=True)
        service = _service(db, FakeZohoCommunications())
        recipient = service.get_view(customer_id=customer.id).recipients[1]

        result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=recipient.key,
            subject="Status",
            content=(
                "<style>.cta { background-color: #297db8; color: #ffffff; "
                "padding: 12px 18px; text-decoration: none; }</style>"
                '<p><a class="cta" href="https://example.de">Direkt aus dem Hub.</a></p>'
            ),
        )

        assert result.success is True
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in delivered_html[0]
        assert "Direkt aus dem Hub." in delivered_html[0]
        assert 'background-color: #297db8' in delivered_html[0]
        assert '<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="https://example.de"' in delivered_html[0]
        outgoing = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.direction == "outbound"))
        assert outgoing is not None
        compiler = service._payload(outgoing.encrypted_payload_json)["email_compiler"]
        assert compiler["mode"] == "hub"
        assert compiler["version"]


def test_customer_communications_resolves_visible_customer_field_placeholders_in_links():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        service.update_email_template(
            template_id="zoho-template-1",
            name="Website-Link",
            subject="Website für ${Customer.Name}",
            content=(
                '<p><a href="${Customer.Website}">Aktuelle Website</a><br>'
                '<a href="${Accounts.Webseite}">Bestehende Vorlage</a><br>'
                '<a href="${Customer.WorkDomain}">Arbeitsdomain</a><br>'
                "${Customer.Update-Notiz}</p>"
            ),
        )

        template = service.get_email_template(customer_id=customer.id, template_id="zoho-template-1")

        assert template.subject == "Website für Example Customer"
        assert template.content.count('href="https://example-customer.de"') == 2
        assert 'href="https://work.example-customer.de"' in template.content
        assert "Bitte nachfassen" in template.content
        assert template.unresolved_placeholders == ()


def test_customer_communications_resolves_visible_contact_field_placeholders():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        service.update_email_template(
            template_id="zoho-template-1",
            name="Kontakt-Anrede",
            subject="Nachricht für ${Contact.Name}",
            content=(
                "<p>${Contact.Briefanrede} ${Contact.Nachname},<br>"
                "${Contact.E-Mail}<br>${Contact.SecondaryEmail}<br>${Contacts.Last_Name}</p>"
            ),
        )
        recipient = next(item for item in service.list_contact_recipients(customer_id=customer.id) if item.key.startswith("contact:"))

        template = service.get_email_template(
            customer_id=customer.id,
            template_id="zoho-template-1",
            recipient_key=recipient.key,
        )

        assert template.subject == "Nachricht für Anna Example"
        assert "Sehr geehrte Frau Example," in template.content
        assert "anna@example.de" in template.content
        assert "anna.private@example.de" in template.content
        assert template.content.count("Example") == 2
        assert template.unresolved_placeholders == ()


def test_customer_communications_resolves_canonical_linked_and_global_placeholders():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        signature = "<p>Mit freundlichen Grüßen</p>"
        EmailComposerSettingsService(db=db).configure_signature(signature_html=signature)
        service.update_email_template(
            template_id="zoho-template-1",
            name="Kanonische Felder",
            subject="Für ${Contact.Name} bei ${Customer.Name}",
            content=(
                "<p>${Contact.Greeting}, ${Contact.FirstName} ${Contact.LastName}<br>"
                "${Contact.Email}<br>${Customer.Website}</p>${Company.EmailSignature}"
            ),
        )
        recipient = next(item for item in service.list_contact_recipients(customer_id=customer.id) if item.key.startswith("contact:"))
        rendered = service.get_email_template(
            customer_id=customer.id,
            template_id="zoho-template-1",
            recipient_key=recipient.key,
        )

        assert rendered.subject == "Für Anna Example bei Example Customer"
        assert "Sehr geehrte Frau Example" in rendered.content
        assert "Anna Example" in rendered.content
        assert "anna@example.de" in rendered.content
        assert "https://example-customer.de" in rendered.content
        assert signature in rendered.content
        assert rendered.unresolved_placeholders == ()


def test_customer_communications_resolves_supplied_dunning_placeholders():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        service.update_email_template(
            template_id="zoho-template-1",
            name="Erneute Abbuchung",
            subject="Mahnung ${Dunning.Number}",
            content=(
                "<p>Wir werden den noch ausstehenden Betrag von ${Dunning.GrossTotal} "
                "am <strong>${Dunning.DueDate}</strong> erneut abbuchen.</p>"
            ),
            context_module="dunnings",
        )

        rendered = service.get_email_template(
            customer_id=customer.id,
            template_id="zoho-template-1",
            template_values={
                "Dunning.Number": "MAH-000002",
                "Dunning.GrossTotal": "534,30 EUR",
                "Dunning.DueDate": "30.09.2026",
            },
        )

        assert rendered.subject == "Mahnung MAH-000002"
        assert "534,30 EUR" in rendered.content
        assert "<strong>30.09.2026</strong>" in rendered.content
        assert rendered.unresolved_placeholders == ()


def test_customer_communications_moves_email_template_to_an_existing_folder():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        service.update_email_template(
            template_id="zoho-template-1",
            name="Statusvorlage",
            subject="Aktueller Stand",
            content="<p>Hallo</p>",
            folder_name="Intern",
        )

        templates = {item.id: item for item in service.list_email_templates()}
        assert templates["zoho-template-1"].category == "Intern"
        assert templates["zoho-template-2"].category == "Kunden"


def test_customer_communications_inserts_the_shared_signature_as_safe_html_in_templates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        signature = '<p>Viele Grüße<br><img src="/emails/compose/images/' + "a" * 32 + '" alt="Kosmos"></p>'
        EmailComposerSettingsService(db=db).configure_signature(signature_html=signature)
        service.update_email_template(
            template_id="zoho-template-1",
            name="Mit Signatur",
            subject="Aktueller Stand",
            content="<p>Hallo</p>${userSignature}",
        )

        source = service.get_email_template_source(template_id="zoho-template-1")
        preview = service.get_email_template_preview(template_id="zoho-template-1")

        assert source.content == "<p>Hallo</p>${userSignature}"
        assert "${userSignature}" not in preview.content
        assert signature in preview.content
        assert "&lt;img" not in preview.content
        assert preview.unresolved_placeholders == ()


def test_customer_communications_clones_and_deletes_local_email_templates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        cloned = service.clone_email_template(
            template_id="zoho-template-1",
            name="Statusvorlage_geklont",
        )
        assert cloned.id.startswith("hub-template-")
        assert cloned.name == "Statusvorlage_geklont"
        assert cloned.subject == "Aktueller Stand für ${Accounts.Account_Name}"
        cloned_summary = next(item for item in service.list_email_templates() if item.id == cloned.id)
        assert cloned_summary.cloned_from == "zoho-template-1"
        assert not cloned_summary.content_reviewed

        cloned_model = db.scalar(
            select(ZohoEmailTemplate).where(ZohoEmailTemplate.zoho_template_id == cloned.id)
        )
        assert cloned_model is not None
        cloned_payload = service._payload(cloned_model.encrypted_payload_json)
        cloned_payload["hub_content_reviewed_at"] = "2026-09-16T12:00:00+00:00"
        cloned_model.encrypted_payload_json = service._encrypt_payload(cloned_payload)
        db.flush()
        reviewed_summary = next(item for item in service.list_email_templates() if item.id == cloned.id)
        assert reviewed_summary.content_reviewed

        _seed_templates(service)
        assert {item.id for item in service.list_email_templates()} == {
            "zoho-template-1",
            "zoho-template-2",
            cloned.id,
        }

        service.delete_email_template(template_id=cloned.id)
        service.delete_email_template(template_id="zoho-template-1")
        _seed_templates(service)
        assert [item.id for item in service.list_email_templates()] == ["zoho-template-2"]


def test_customer_communications_moves_and_deletes_email_templates_in_batches():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        cloned = service.clone_email_template(
            template_id="zoho-template-1",
            name="Statusvorlage Kopie",
        )

        moved = service.move_email_templates(
            template_ids=("zoho-template-1", cloned.id, "zoho-template-1"),
            folder_name="Intern",
        )
        templates = {item.id: item for item in service.list_email_templates()}

        assert moved == 2
        assert templates["zoho-template-1"].category == "Intern"
        assert templates[cloned.id].category == "Intern"
        assert templates["zoho-template-2"].category == "Kunden"

        deleted = service.delete_email_templates(template_ids=("zoho-template-1", cloned.id))

        assert deleted == 2
        assert [item.id for item in service.list_email_templates()] == ["zoho-template-2"]


def test_customer_communications_persists_and_infers_email_template_context_modules():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        _seed_templates(service)
        assert service.get_email_template_source(template_id="zoho-template-1").context_module == "customers"

        edited = service.update_email_template(
            template_id="zoho-template-1",
            name="Mahnung senden",
            subject="Mahnung ${Dunning.Number}",
            content="<p>Rechnung ${Dunning.SourceInvoiceNumber}</p>",
            context_module="dunnings",
        )
        assert edited.context_module == "dunnings"
        cloned = service.clone_email_template(template_id="zoho-template-1", name="Mahnung senden Kopie")
        assert cloned.context_module == "dunnings"

        with pytest.raises(ValueError, match="Kontextmodul"):
            service.update_email_template(
                template_id="zoho-template-1",
                name="Ungültig",
                subject="Test",
                content="<p>Test</p>",
                context_module="unknown-module",
            )


def test_customer_communication_view_prepares_a_sandboxed_html_email_preview():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, _contact = _customer(cipher)
        db.add(customer)
        db.commit()
        db.add(
            CustomerZohoEmail(
                customer=customer,
                source="zoho",
                direction="inbound",
                sync_status="synced",
                encrypted_payload_json=cipher.encrypt(
                    '{"subject":"HTML mail","content":"<html><head><style>p { color: red; }</style></head><body><p>Hallo <strong>Anna</strong>,</p><p>deine Anfrage ist angekommen.</p><script>alert(1)</script></body></html>"}'
                ),
            )
        )
        db.commit()

        view = _service(db, FakeZohoCommunications()).get_view(customer_id=customer.id)

        preview = view.emails[0].preview_html
        assert preview is not None
        assert "<style>p { color: red; }</style>" in preview
        assert "Hallo <strong>Anna</strong>" in preview
        assert "default-src 'none'" in preview
        assert "img-src 'self' data:" in preview


def test_customer_communication_preview_caches_external_images_for_thirty_days():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, _contact = _customer(cipher)
        db.add(customer)
        db.commit()
        source_url = "https://images.example.test/logo.png"
        email = CustomerZohoEmail(
            customer=customer,
            source="zoho",
            direction="inbound",
            sync_status="synced",
            encrypted_payload_json=cipher.encrypt(
                json.dumps({"subject": "Image mail", "content": f'<p><img src="{source_url}" alt="Logo"></p>'})
            ),
        )
        db.add(email)
        db.commit()

        service = _service(db, FakeZohoCommunications())
        source_url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        preview = service.get_view(customer_id=customer.id).emails[0].preview_html
        assert preview is not None
        assert source_url not in preview
        assert f"/customers/{customer.id}/communications/emails/{email.id}/images/{source_url_hash}" in preview

        downloads: list[str] = []
        service._download_external_image = lambda source: (downloads.append(source) or (b"image-bytes", "image/png"))  # type: ignore[method-assign]
        first = service.get_email_preview_image(
            customer_id=customer.id,
            email_id=email.id,
            source_url_hash=source_url_hash,
        )
        db.commit()
        assert first.content == b"image-bytes"
        assert first.content_type == "image/png"
        assert downloads == [source_url]

        cached_image = db.scalar(select(CustomerZohoEmailImage))
        assert cached_image is not None
        assert b"image-bytes" not in cached_image.encrypted_image_bytes
        assert CustomerCommunicationService._as_utc(cached_image.expires_at) > datetime.now(UTC)

        second = service.get_email_preview_image(
            customer_id=customer.id,
            email_id=email.id,
            source_url_hash=source_url_hash,
        )
        assert second.content == b"image-bytes"
        assert downloads == [source_url]

        cached_image.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        refreshed = service.get_email_preview_image(
            customer_id=customer.id,
            email_id=email.id,
            source_url_hash=source_url_hash,
        )
        assert refreshed.content == b"image-bytes"
        assert downloads == [source_url, source_url]


def test_customer_communication_preview_accepts_public_http_image_sources():
    assert CustomerCommunicationService._is_external_image_source("http://images.example.test/logo.png") is True
    assert CustomerCommunicationService._is_external_image_source("http://127.0.0.1/logo.png") is False
    assert CustomerCommunicationService._is_external_image_source("http://images.example.test:8080/logo.png") is False
