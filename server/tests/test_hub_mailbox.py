import json
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.core.templates import _unread_email_count_for_db, create_templates
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.hub_mailbox import HubMailboxService


def test_mailbox_combines_customer_email_and_unassigned_workflow_email():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="Example GmbH", zoho_id="zoho-account-1")
        linked_email = CustomerZohoEmail(
            customer=customer,
            zoho_message_id="zoho-email-1",
            source="zoho",
            direction="inbound",
            is_unread=True,
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "subject": "Bekannte E-Mail",
                        "from": {"email": "known@example.de"},
                        "to": [{"email": "team@example.de"}],
                    }
                )
            ),
            zoho_sent_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
        )
        unassigned_email = HubMailboxEmail(
            direction="inbound",
            is_unread=True,
            fingerprint="a" * 64,
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "betreff": "Noch unbekannt",
                        "absender": "new@example.de",
                        "empfaenger": "team@example.de",
                        "content": "<p>Neue Anfrage <strong>mit Inhalt</strong>.</p>",
                    }
                )
            ),
            received_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
        )
        db.add_all([customer, linked_email, unassigned_email])
        db.commit()

        service = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        full_view_calls: list[int] = []
        original_email_view = service.communications._email_view

        def track_full_view(email: CustomerZohoEmail):
            full_view_calls.append(email.id)
            return original_email_view(email)

        service.communications._email_view = track_full_view
        inbox = service.get_view(folder="inbox", unread_only=False)

        assert inbox.folder_counts == {"inbox": 2, "sent": 0, "unassigned": 1}
        assert [message.subject for message in inbox.messages] == ["Noch unbekannt", "Bekannte E-Mail"]
        assert full_view_calls == []
        assert not hasattr(inbox.messages[1], "preview_html")
        assert inbox.selected is not None
        assert inbox.selected.kind == "unassigned"
        assert "Neue Anfrage <strong>mit Inhalt</strong>" in (inbox.selected.preview_html or "")
        assert "default-src 'none'" in (inbox.selected.preview_html or "")
        assert inbox.messages[1].customers[0].name == "Example GmbH"

        template = create_templates(directory=str(Path(__file__).resolve().parents[1] / "app" / "templates"))
        rendered_list = template.get_template("emails_message_list.html").render(
            messages=inbox.messages,
            selected=None,
            folder="inbox",
            unread=False,
        )
        serialized_messages = re.search(r'<script type="application/json" data-mailbox-list-data>(.*?)</script>', rendered_list)
        assert serialized_messages is not None
        assert json.loads(serialized_messages.group(1))[0]["subject"] == "Noch unbekannt"

        folder_view = service.get_folder_view(folder="inbox", unread_only=False)
        assert [message.subject for message in folder_view.messages] == ["Noch unbekannt", "Bekannte E-Mail"]
        assert folder_view.selected is not None
        assert folder_view.selected.subject == "Noch unbekannt"

        linked_selected = service.get_selected_message(
            folder="inbox",
            unread_only=False,
            selected_key=f"linked-{customer.id}-{linked_email.id}",
        )
        assert linked_selected is not None
        assert linked_selected.subject == "Bekannte E-Mail"
        assert linked_selected.customers[0].name == "Example GmbH"

        unassigned_selected = service.get_selected_message(
            folder="unassigned",
            unread_only=False,
            selected_key=f"unassigned-{unassigned_email.id}",
        )
        assert unassigned_selected is not None
        assert unassigned_selected.subject == "Noch unbekannt"
        assert "Neue Anfrage <strong>mit Inhalt</strong>" in (unassigned_selected.preview_html or "")
        assert (
            service.get_selected_message(
                folder="sent",
                unread_only=False,
                selected_key=f"linked-{customer.id}-{linked_email.id}",
            )
            is None
        )

        service.mark_unassigned_read(email_id=unassigned_email.id)
        unread = service.get_view(folder="inbox", unread_only=True)
        assert [message.subject for message in unread.messages] == ["Bekannte E-Mail"]


def test_customer_email_message_id_has_a_dedicated_index():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    index_names = {index["name"] for index in inspect(engine).get_indexes("customer_zoho_emails")}

    assert "ix_customer_zoho_emails_zoho_message_id" in index_names
    assert "ix_customer_zoho_emails_direction_is_unread_message_id" in index_names


def test_unread_email_count_uses_message_identity_without_loading_email_views():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        first_customer = Customer(name="First GmbH", zoho_id="zoho-account-1")
        second_customer = Customer(name="Second GmbH", zoho_id="zoho-account-2")
        db.add_all(
            [
                CustomerZohoEmail(
                    customer=first_customer,
                    zoho_message_id="shared-message",
                    source="zoho",
                    direction="inbound",
                    is_unread=True,
                    encrypted_payload_json=cipher.encrypt(json.dumps({"content": "first"})),
                ),
                CustomerZohoEmail(
                    customer=second_customer,
                    zoho_message_id="shared-message",
                    source="zoho",
                    direction="inbound",
                    is_unread=True,
                    encrypted_payload_json=cipher.encrypt(json.dumps({"content": "second"})),
                ),
                CustomerZohoEmail(
                    customer=first_customer,
                    source="zoho",
                    direction="inbound",
                    is_unread=True,
                    encrypted_payload_json=cipher.encrypt(json.dumps({"content": "local"})),
                ),
                CustomerZohoEmail(
                    customer=first_customer,
                    zoho_message_id="read-message",
                    source="zoho",
                    direction="inbound",
                    is_unread=False,
                    encrypted_payload_json=cipher.encrypt(json.dumps({"content": "read"})),
                ),
                HubMailboxEmail(
                    direction="inbound",
                    is_unread=True,
                    fingerprint="b" * 64,
                    encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Unassigned"})),
                    received_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
                ),
            ]
        )
        db.commit()

        assert _unread_email_count_for_db(db) == 3
