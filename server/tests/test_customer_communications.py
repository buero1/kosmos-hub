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
from app.services.customer_communications import CustomerCommunicationAttachmentUpload, CustomerCommunicationService
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.email_composer_settings import EmailComposerSettingsService
from app.services.hub_mailbox_transport import HubMailboxTransportDelivery
from app.services.zoho_crm import ZohoCrmError


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


def test_note_title_uses_the_first_nonempty_content_line_when_omitted():
    title = CustomerCommunicationService._note_title_from_content("\n  Anfrage zur Rechnung\nWeitere Details")

    assert title == "Anfrage zur Rechnung"


def _service(
    db: Session,
    fake_zoho: FakeZohoCommunications,
    attachment_storage: EmailAttachmentStorage | None = None,
) -> CustomerCommunicationService:
    return CustomerCommunicationService(
        db=db,
        cipher=SecretCipher("a" * 32),
        public_base_url="https://hub.example",
        zoho_service=fake_zoho,  # type: ignore[arg-type]
        attachment_storage=attachment_storage,
    )


def _customer(cipher: SecretCipher) -> tuple[Customer, CustomerContact]:
    customer = Customer(
        name="Example Customer",
        zoho_id="zoho-account-1",
        encrypted_profile_json=cipher.encrypt(
            json.dumps(
                {
                    "fields": {
                        "Kontakt-E-Mail": "accounts@example.de",
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


def test_customer_communications_syncs_headers_and_keeps_payloads_encrypted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        result = service.sync_customer(customer_id=customer.id)
        db.commit()

        assert (result.notes, result.emails) == (1, 1)
        stored_note = db.scalar(select(CustomerZohoNote))
        stored_email = db.scalar(select(CustomerZohoEmail))
        assert stored_note is not None
        assert stored_email is not None
        assert stored_email.is_unread is False
        assert "Imported note content" not in stored_note.encrypted_payload_json
        assert "client@example.de" not in stored_email.encrypted_payload_json

        view = service.get_view(customer_id=customer.id)
        assert view.notes[0].content == "Imported note content"
        assert view.emails[0].subject == "Anfrage"
        assert view.emails[0].preview_html is None
        assert [(item.id, item.filename) for item in view.emails[0].attachments] == [("zoho-attachment-1", "Angebot.pdf")]
        assert view.emails[0].can_load_content is True
        assert [recipient.email for recipient in view.recipients] == [
            "accounts@example.de",
            "anna@example.de",
            "anna.private@example.de",
        ]

        second = service.sync_customer(customer_id=customer.id)
        assert (second.notes, second.emails) == (0, 0)


def test_customer_communications_marks_webhook_emails_unread_until_opened():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        service.sync_customer(customer_id=customer.id, mark_new_emails_unread=True)
        email = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-account-1"))
        assert email is not None
        assert email.is_unread is True

        service.mark_email_read(customer_id=customer.id, email_id=email.id)

        assert email.is_unread is False


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


def test_customer_communications_auto_loads_new_webhook_email_content_without_marking_it_read():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        result = service.sync_customer_email_headers(
            customer_id=customer.id,
            mark_new_emails_unread=True,
            load_new_inbound_content=True,
        )
        imported = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.customer_id == customer.id))

        assert result.emails == 1
        assert result.loaded_contents == 1
        assert imported is not None
        assert imported.is_unread is True
        assert service._payload(imported.encrypted_payload_json)["content"] == "Full imported email body"
        assert len(fake_zoho.email_content_requests) == 1


def test_customer_communications_treats_zoho_unsent_headers_as_inbound():
    assert CustomerCommunicationService._email_direction({"sent": False}) == "inbound"


def test_customer_communications_downloads_only_the_selected_email_attachment():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        service.sync_customer(customer_id=customer.id)
        email = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-account-1"))
        assert email is not None

        download = service.download_email_attachment(
            customer_id=customer.id,
            email_id=email.id,
            attachment_id="zoho-attachment-1",
        )

        assert download.content == b"%PDF-test"
        assert download.content_type == "application/pdf"
        assert download.filename == "Angebot.pdf"
        assert fake_zoho.downloaded_attachments == [
            ("Accounts", "zoho-account-1", "zoho-email-account-1", "zoho-owner-1", "zoho-attachment-1", "Angebot.pdf")
        ]
        with pytest.raises(ValueError, match="gehört nicht"):
            service.download_email_attachment(
                customer_id=customer.id,
                email_id=email.id,
                attachment_id="other-attachment",
            )


def test_customer_communications_stores_an_email_attachment_encrypted_and_reuses_it(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        storage = EmailAttachmentStorage(root=tmp_path / "attachments", cipher=cipher, min_free_bytes=0)
        service = _service(db, fake_zoho, attachment_storage=storage)
        service.sync_customer(customer_id=customer.id)
        email = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-account-1"))
        assert email is not None

        stored = service.store_email_attachment(
            customer_id=customer.id,
            email_id=email.id,
            attachment_id="zoho-attachment-1",
        )
        db.commit()

        assert stored.byte_size == len(b"%PDF-test")
        assert db.scalar(select(CustomerEmailAttachment).where(CustomerEmailAttachment.id == stored.id)) is not None
        encrypted_blob = next((tmp_path / "attachments").rglob("*.bin"))
        assert b"%PDF-test" not in encrypted_blob.read_bytes()

        download = service.download_email_attachment(
            customer_id=customer.id,
            email_id=email.id,
            attachment_id="zoho-attachment-1",
        )
        assert download.content == b"%PDF-test"
        assert fake_zoho.downloaded_attachments == [
            ("Accounts", "zoho-account-1", "zoho-email-account-1", "zoho-owner-1", "zoho-attachment-1", "Angebot.pdf")
        ]


def test_customer_communications_create_notes_send_mail_and_load_bodies():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        service.sync_customer(customer_id=customer.id)
        view = service.get_view(customer_id=customer.id)

        note_result = service.create_note(
            customer_id=customer.id,
            actor="operator",
            title="Hub note",
            content="Created from the Hub",
        )
        assert note_result.success is True
        assert fake_zoho.created_notes == [("zoho-account-1", "Hub note", "Created from the Hub")]


        email_result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=view.recipients[1].key,
            subject="Status",
            content="Created from the Hub",
        )
        assert email_result.success is True
        sent_email = fake_zoho.sent_emails[0]
        assert sent_email[:6] == (
            "zoho-account-1",
            "Hub Team",
            "team@example.de",
            "Anna Example",
            "anna@example.de",
            "Status",
        )
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in sent_email[6]
        assert "Created from the Hub" in sent_email[6]
        assert sent_email[7] is None

        imported_email = db.scalar(
            select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-account-1")
        )
        assert imported_email is not None
        load_result = service.load_email_content(customer_id=customer.id, email_id=imported_email.id)
        assert load_result.success is True
        assert fake_zoho.email_content_requests == ["zoho-owner-1"]
        updated_view = service.get_view(customer_id=customer.id)
        assert "Created from the Hub" in (updated_view.emails[0].preview_html or "")
        assert any("Full imported email body" in (email.preview_html or "") for email in updated_view.emails)


def test_customer_communications_update_and_delete_notes_in_zoho():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        service.sync_customer(customer_id=customer.id)
        note = db.scalar(select(CustomerZohoNote).where(CustomerZohoNote.zoho_note_id == "zoho-note-1"))
        assert note is not None

        result = service.update_note(
            customer_id=customer.id,
            note_id=note.id,
            title="Aktualisierte Notiz",
            content="Aktualisierter Inhalt",
        )
        assert result.success is True
        assert fake_zoho.updated_notes == [("zoho-note-1", "Aktualisierte Notiz", "Aktualisierter Inhalt")]
        view = service.get_view(customer_id=customer.id)
        assert view.notes[0].title == "Aktualisierte Notiz"
        assert view.notes[0].content == "Aktualisierter Inhalt"

        delete_result = service.delete_note(customer_id=customer.id, note_id=note.id)
        assert delete_result.success is True
        assert fake_zoho.deleted_notes == ["zoho-note-1"]
        assert db.get(CustomerZohoNote, note.id) is None


def test_customer_communications_keeps_loaded_email_content_during_header_sync():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        service.sync_customer(customer_id=customer.id)
        email = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-account-1"))
        assert email is not None
        service.load_email_content(customer_id=customer.id, email_id=email.id)
        db.commit()

        service.sync_customer(customer_id=customer.id)
        payload = service._payload(email.encrypted_payload_json)

        assert payload["content"] == "Full imported email body"
        assert payload["attachments"] == [{"id": "zoho-attachment-1", "name": "Angebot.pdf"}]


def test_customer_communications_rejects_an_email_response_without_content():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        service.sync_customer(customer_id=customer.id)
        email = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-account-1"))
        assert email is not None
        fake_zoho.get_record_email = lambda **_kwargs: {"id": "zoho-email-account-1"}  # type: ignore[method-assign]

        with pytest.raises(ZohoCrmError, match="ohne Inhalt"):
            service.load_email_content(customer_id=customer.id, email_id=email.id)


def test_customer_communications_replies_to_the_original_zoho_message():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()
        original = CustomerZohoEmail(
            customer=customer,
            zoho_message_id="zoho-parent-message-1",
            zoho_module="Accounts",
            zoho_record_id="zoho-account-1",
            source="zoho",
            direction="inbound",
            sync_status="synced",
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "subject": "Frage zur Rechnung",
                        "owner": {"id": "zoho-owner-1"},
                        "from": {"name": "Anna Example", "email": "anna@example.de"},
                        "to": [{"name": "Hub Team", "email": "team@example.de"}],
                        "cc": [{"name": "Office", "email": "office@example.de"}],
                        "content": "<p>Originale Nachricht</p>",
                    }
                )
            ),
        )
        db.add(original)
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        signature = "<p>Viele Grüße<br>Hub Team</p>"
        EmailComposerSettingsService(db=db).configure_signature(signature_html=signature)
        reply = service.get_email_reply(customer_id=customer.id, email_id=original.id)

        assert reply.recipient_key.startswith(f"contact:{contact.id}:")
        assert reply.recipient_email == "anna@example.de"
        assert reply.subject == "Re: Frage zur Rechnung"
        assert "Am " in reply.content
        assert "Anna Example" in reply.content
        assert "Originale Nachricht" in reply.content
        assert "<strong>Am " in reply.content
        assert reply.content.startswith(f"<p><br><br></p>{signature}<p><br><br></p><p><strong>Am ")
        assert "<blockquote" in reply.content
        assert reply.reply_all_cc_emails == ("office@example.de", "team@example.de")

        result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=reply.recipient_key,
            subject=reply.subject,
            content="Danke, wir melden uns.",
            reply_to_email_id=original.id,
            cc_emails=", ".join(reply.reply_all_cc_emails),
        )

        assert result.success is True
        assert fake_zoho.reply_to_calls == [("zoho-parent-message-1", "zoho-owner-1")]
        assert fake_zoho.sent_cc_recipients == [(("office@example.de", "office@example.de"),)]
        sent_email = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-hub-1"))
        assert sent_email is not None
        assert service._payload(sent_email.encrypted_payload_json)["in_reply_to"] == {
            "email_id": original.id,
            "message_id": "zoho-parent-message-1",
        }


def test_customer_communications_forwards_loaded_content_and_attachments():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()
        original = CustomerZohoEmail(
            customer=customer,
            zoho_message_id="zoho-forward-message-1",
            zoho_module="Accounts",
            zoho_record_id="zoho-account-1",
            source="zoho",
            direction="inbound",
            sync_status="synced",
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "subject": "Angebot",
                        "owner": {"id": "zoho-owner-1"},
                        "from": {"name": "Anna Example", "email": "anna@example.de"},
                        "to": [{"name": "Hub Team", "email": "team@example.de"}],
                        "attachments": [{"id": "zoho-attachment-1", "name": "Angebot.pdf"}],
                    }
                )
            ),
        )
        db.add(original)
        db.commit()

        fake_zoho = FakeZohoCommunications()
        fake_zoho.get_record_email = lambda **_kwargs: {
            "id": "zoho-forward-message-1",
            "content": "<p>Bitte das Angebot weiterleiten.</p>",
        }  # type: ignore[method-assign]
        service = _service(db, fake_zoho)
        forward = service.get_email_forward(customer_id=customer.id, email_id=original.id)
        recipient = service.get_view(customer_id=customer.id).recipients[1]

        assert forward.subject == "Fwd: Angebot"
        assert "Weitergeleitete Nachricht" in forward.content
        assert service._payload(original.encrypted_payload_json)["content"] == "<p>Bitte das Angebot weiterleiten.</p>"
        result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=recipient.key,
            subject=forward.subject,
            content=forward.content,
            forward_from_email_id=original.id,
        )

        assert result.success is True
        assert fake_zoho.uploaded_files == [("Angebot.pdf", b"%PDF-test", "application/pdf")]
        assert fake_zoho.sent_attachment_ids == [("zfs-1",)]


def test_customer_communications_sends_new_email_attachments():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        recipient = service.get_view(customer_id=customer.id).recipients[1]
        result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=recipient.key,
            subject="Unterlagen",
            content="Anbei die Unterlagen.",
            attachments=(
                CustomerCommunicationAttachmentUpload(
                    filename="Unterlagen.txt",
                    content=b"Anbei",
                    content_type="text/plain",
                ),
            ),
        )

        assert result.success is True
        assert fake_zoho.uploaded_files == [("Unterlagen.txt", b"Anbei", "text/plain")]
        assert fake_zoho.sent_attachment_ids == [("zfs-1",)]


def test_customer_communications_sends_sanitized_rich_email_html():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        view = service.get_view(customer_id=customer.id)
        result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=view.recipients[1].key,
            subject="Formatierte Nachricht",
            content=(
                '<div style="text-align: center; color: red"><font color="#0e7c66" size="5">'
                "<strong>Wichtig</strong></font><script>ignored()</script>"
                '<a href="javascript:alert(1)">Unsicher</a><a href="https://example.de">Sicher</a></div>'
            ),
        )

        assert result.success is True
        sent_html = fake_zoho.sent_emails[0][-2]
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in sent_html
        assert '<div style="text-align: center; color: red"><font color="#0e7c66" size="5"><strong>Wichtig</strong></font>' in sent_html
        assert 'javascript:alert' not in sent_html


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


def test_customer_communications_loads_and_sends_an_editable_zoho_email_template():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        sync_result = service.sync_email_templates()
        assert (sync_result.created, sync_result.updated, sync_result.archived) == (2, 0, 0)
        assert fake_zoho.template_detail_requests == ["zoho-template-1", "zoho-template-2"]
        assert [(item.id, item.name, item.module) for item in service.list_email_templates()] == [
            ("zoho-template-1", "Statusvorlage", "Accounts"),
            ("zoho-template-2", "Kontaktvorlage", "Contacts"),
        ]
        assert [item.category for item in service.list_email_templates()] == ["Kunden", "Kunden"]
        preview = service.get_email_template_preview(template_id="zoho-template-1")
        assert preview.subject == "Aktueller Stand für ${Accounts.Account_Name}"
        assert "${!Accounts.id}" in preview.content
        assert preview.unresolved_placeholders == (
            "!Accounts.Dialfire_WV_Datum",
            "!Accounts.Dialfire_WV_Notiz",
            "!Accounts.id",
            "Accounts.Account_Name",
        )
        template = service.get_email_template(customer_id=customer.id, template_id="zoho-template-1")
        assert template.subject == "Aktueller Stand für Example Customer"
        assert template.content == '<table style="width: 100%"><tr><td><strong>Hallo Example Customer zoho-account-1 2026-09-02 Bitte nachfassen</strong></td></tr></table>'
        assert template.unresolved_placeholders == ()
        assert fake_zoho.template_detail_requests == ["zoho-template-1", "zoho-template-2"]

        recipient = service.get_view(customer_id=customer.id).recipients[0]
        result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=recipient.key,
            subject="Ergänzter Stand",
            content=template.content + "<p>Mit Zusatz.</p>",
            template_id=template.id,
        )
        assert result.success is True
        assert fake_zoho.sent_emails[0][-1] is None
        assert '<meta name="viewport"' not in fake_zoho.sent_emails[0][-2]
        outgoing = db.scalar(
            select(CustomerZohoEmail).where(CustomerZohoEmail.direction == "outbound")
        )
        assert outgoing is not None
        assert service._payload(outgoing.encrypted_payload_json)["email_compiler"] == {
            "mode": "legacy",
            "version": None,
            "warnings": [],
        }
        assert fake_zoho.template_detail_requests == ["zoho-template-1", "zoho-template-2"]


def test_customer_communications_resolves_visible_customer_field_placeholders_in_links():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        service.sync_email_templates()
        service.update_email_template(
            template_id="zoho-template-1",
            name="Website-Link",
            subject="Website für ${Customer.Name}",
            content=(
                '<p><a href="${Customer.Website}">Aktuelle Website</a><br>'
                '<a href="${Accounts.Webseite}">Bestehende Vorlage</a><br>'
                "${Customer.Update-Notiz}</p>"
            ),
        )

        template = service.get_email_template(customer_id=customer.id, template_id="zoho-template-1")

        assert template.subject == "Website für Example Customer"
        assert template.content.count('href="https://example-customer.de"') == 2
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
        service.sync_email_templates()
        service.update_email_template(
            template_id="zoho-template-1",
            name="Kontakt-Anrede",
            subject="Nachricht für ${Contact.Name}",
            content=(
                "<p>${Contact.Briefanrede} ${Contact.Nachname},<br>"
                "${Contact.E-Mail}<br>${Contacts.Last_Name}</p>"
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
        assert template.content.count("Example") == 2
        assert template.unresolved_placeholders == ()


def test_customer_communications_keeps_hub_template_edits_when_zoho_templates_are_synced():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        service.sync_email_templates()
        edited = service.update_email_template(
            template_id="zoho-template-1",
            name="Eigene Statusvorlage",
            subject="Eigener Betreff für ${Accounts.Account_Name}",
            content="<p>Hallo ${Accounts.Account_Name}</p>",
        )
        assert edited.name == "Eigene Statusvorlage"
        assert edited.subject == "Eigener Betreff für ${Accounts.Account_Name}"

        sync_result = service.sync_email_templates()
        assert (sync_result.created, sync_result.updated, sync_result.archived) == (0, 0, 0)
        preview = service.get_email_template_preview(template_id="zoho-template-1")
        assert preview.name == "Eigene Statusvorlage"
        assert preview.content == "<p>Hallo ${Accounts.Account_Name}</p>"


def test_customer_communications_inserts_the_shared_signature_as_safe_html_in_templates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        service = _service(db, FakeZohoCommunications())
        service.sync_email_templates()
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
        service.sync_email_templates()
        cloned = service.clone_email_template(
            template_id="zoho-template-1",
            name="Statusvorlage_geklont",
        )
        assert cloned.id.startswith("hub-template-")
        assert cloned.name == "Statusvorlage_geklont"
        assert cloned.subject == "Aktueller Stand für ${Accounts.Account_Name}"

        service.sync_email_templates()
        assert {item.id for item in service.list_email_templates()} == {
            "zoho-template-1",
            "zoho-template-2",
            cloned.id,
        }

        service.delete_email_template(template_id=cloned.id)
        service.delete_email_template(template_id="zoho-template-1")
        service.sync_email_templates()
        assert [item.id for item in service.list_email_templates()] == ["zoho-template-2"]


def test_customer_communication_sync_deduplicates_one_message_linked_to_account_and_contact():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        duplicate_header = {
            "message_id": "zoho-shared-email-1",
            "subject": "Shared history item",
            "sent": True,
            "time": "2026-09-02T08:20:00+00:00",
        }
        fake_zoho.list_record_email_headers = lambda _module, _record_id: [duplicate_header]
        result = _service(db, fake_zoho).sync_customer(customer_id=customer.id)

        assert result.emails == 1
        assert db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-shared-email-1")) is not None


def test_customer_communication_sync_deduplicates_account_and_contact_copies_with_distinct_zoho_ids():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact = _customer(cipher)
        db.add_all([customer, contact])
        db.commit()

        fake_zoho = FakeZohoCommunications()
        account_header = {
            "message_id": "zoho-account-copy-1",
            "subject": "Website fertiggestellt",
            "from": {"name": "Team Kosmos", "email": "info@kosmos-medien.de"},
            "to": [{"name": "Customer", "email": "customer@example.de"}],
            "sent_time": "2026-09-02T09:30:00+00:00",
        }
        contact_header = {
            "message_id": "zoho-contact-copy-1",
            "subject": "Website fertiggestellt",
            "from": {"name": "info@kosmos-medien.de", "email": "info@kosmos-medien.de"},
            "to": [{"name": "Customer", "email": "customer@example.de"}],
            "sent_time": "2026-09-02T09:30:01+00:00",
        }
        fake_zoho.list_record_email_headers = lambda module, _record_id: [
            account_header if module == "Accounts" else contact_header
        ]

        result = _service(db, fake_zoho).sync_customer(customer_id=customer.id)

        emails = db.scalars(select(CustomerZohoEmail)).all()
        assert result.emails == 1
        assert len(emails) == 1
        assert emails[0].zoho_message_id == "zoho-contact-copy-1"
        assert emails[0].zoho_module == "Contacts"


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


def test_customer_communication_preview_downloads_zoho_inline_images():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, _contact = _customer(cipher)
        db.add(customer)
        db.commit()
        image_id = "zoho-inline-image-1"
        email = CustomerZohoEmail(
            customer=customer,
            zoho_message_id="zoho-email-inline-1",
            zoho_module="Accounts",
            zoho_record_id="zoho-account-1",
            source="zoho",
            direction="inbound",
            sync_status="synced",
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "subject": "Inline image mail",
                        "owner": {"id": "zoho-owner-1"},
                        "content": f'<p><img src="crm\\img_id:{image_id}" alt="Logo"></p>',
                    }
                )
            ),
        )
        db.add(email)
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        source_url_hash = hashlib.sha256(f"zoho-inline:{image_id}".encode("utf-8")).hexdigest()
        preview = service.get_view(customer_id=customer.id).emails[0].preview_html
        assert preview is not None
        assert f"/customers/{customer.id}/communications/emails/{email.id}/images/{source_url_hash}" in preview

        image = service.get_email_preview_image(
            customer_id=customer.id,
            email_id=email.id,
            source_url_hash=source_url_hash,
        )

        assert image.content == b"inline-image-bytes"
        assert image.content_type == "image/png"
        assert fake_zoho.downloaded_inline_images == [
            ("Accounts", "zoho-account-1", "zoho-email-inline-1", "zoho-owner-1", image_id)
        ]
