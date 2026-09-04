import json
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail, CustomerZohoEmailImage, CustomerZohoNote
from app.models.customer_contact import CustomerContact
from app.services.customer_communications import CustomerCommunicationService
from app.services.zoho_crm import ZohoCrmError


class FakeZohoCommunications:
    def __init__(self):
        self.created_notes: list[tuple[str, str, str]] = []
        self.sent_emails: list[tuple[str, str, str, str, str, str, str, str | None]] = []
        self.reply_to_calls: list[tuple[str | None, str | None]] = []
        self.template_detail_requests: list[str] = []
        self.downloaded_attachments: list[tuple[str, str, str, str, str, str]] = []
        self.downloaded_inline_images: list[tuple[str, str, str, str, str]] = []
        self.email_content_requests: list[str | None] = []

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
    ) -> dict[str, object]:
        self.sent_emails.append((account_id, sender_name, sender_email, recipient_name, recipient_email, subject, content, template_id))
        self.reply_to_calls.append((reply_to_message_id, reply_to_owner_id))
        return {"message_id": "zoho-email-hub-1"}


def test_note_title_uses_the_first_nonempty_content_line_when_omitted():
    title = CustomerCommunicationService._note_title_from_content("\n  Anfrage zur Rechnung\nWeitere Details")

    assert title == "Anfrage zur Rechnung"


def _service(db: Session, fake_zoho: FakeZohoCommunications) -> CustomerCommunicationService:
    return CustomerCommunicationService(
        db=db,
        cipher=SecretCipher("a" * 32),
        public_base_url="https://hub.example",
        zoho_service=fake_zoho,  # type: ignore[arg-type]
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


        with pytest.raises(ValueError, match="Bestätige"):
            service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=view.recipients[0].key,
                subject="Status",
                content="No confirmation",
                confirmed=False,
            )

        email_result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=view.recipients[1].key,
            subject="Status",
            content="Created from the Hub",
            confirmed=True,
        )
        assert email_result.success is True
        assert fake_zoho.sent_emails == [
            ("zoho-account-1", "Hub Team", "team@example.de", "Anna Example", "anna@example.de", "Status", "Created from the Hub", None)
        ]

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
                    }
                )
            ),
        )
        db.add(original)
        db.commit()

        fake_zoho = FakeZohoCommunications()
        service = _service(db, fake_zoho)
        reply = service.get_email_reply(customer_id=customer.id, email_id=original.id)

        assert reply.recipient_key.startswith(f"contact:{contact.id}:")
        assert reply.subject == "Re: Frage zur Rechnung"

        result = service.send_email(
            customer_id=customer.id,
            actor="operator",
            sender_email="team@example.de",
            recipient_key=reply.recipient_key,
            subject=reply.subject,
            content="Danke, wir melden uns.",
            confirmed=True,
            reply_to_email_id=original.id,
        )

        assert result.success is True
        assert fake_zoho.reply_to_calls == [("zoho-parent-message-1", "zoho-owner-1")]
        sent_email = db.scalar(select(CustomerZohoEmail).where(CustomerZohoEmail.zoho_message_id == "zoho-email-hub-1"))
        assert sent_email is not None
        assert service._payload(sent_email.encrypted_payload_json)["in_reply_to"] == {
            "email_id": original.id,
            "message_id": "zoho-parent-message-1",
        }


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
            confirmed=True,
        )

        assert result.success is True
        assert fake_zoho.sent_emails[0][-2] == (
            '<div style="text-align: center; color: red"><font color="#0e7c66" size="5"><strong>Wichtig</strong></font>'
            'Unsicher<a href="https://example.de">Sicher</a></div>'
        )


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
            confirmed=True,
            template_id=template.id,
        )
        assert result.success is True
        assert fake_zoho.sent_emails[0][-1] is None
        assert fake_zoho.template_detail_requests == ["zoho-template-1", "zoho-template-2"]


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
