import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.core.templates import _unread_email_count_for_db, create_templates
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_email import HubMailboxAttachment, HubMailboxEmail
from app.services.customer_communications import CustomerCommunicationAttachmentUpload
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.hub_mailbox import HubMailboxService


def test_global_mailbox_composer_applies_the_saved_default_sender_on_every_open():
    template = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert "if (!globalMailboxSender || globalMailboxSender.value) return;" in template
    assert "globalMailboxApplyNewEmailDefaults();\n                globalMailboxApplyDefaultSender();" in template
    assert "globalMailboxLoadOptions().then(function () {\n              globalMailboxApplyDefaultSender();" in template


def test_email_composers_show_twenty_message_lines():
    main_composer = Path("app/templates/emails.html").read_text(encoding="utf-8")
    global_composer = Path("app/templates/partials/global_mailbox_composer.html").read_text(encoding="utf-8")
    base_template = Path("app/templates/base.html").read_text(encoding="utf-8")

    expected_editor = 'rows="20" data-email-compose-content'
    assert expected_editor in main_composer
    assert expected_editor in global_composer
    assert ".email-compose-form .email-rich-editor .jodit-wysiwyg_iframe" in base_template
    assert "min-height: 264px !important;" in base_template


def test_email_composers_offer_reviewable_ai_rewrites_for_selected_text():
    main_composer = Path("app/templates/emails.html").read_text(encoding="utf-8")
    global_composer = Path("app/templates/partials/global_mailbox_composer.html").read_text(encoding="utf-8")
    base_template = Path("app/templates/base.html").read_text(encoding="utf-8")

    expected_prompt = 'data-email-ai-prompt aria-label="KI-E-Mail-Überarbeitung"'
    assert expected_prompt in main_composer
    assert expected_prompt in global_composer
    assert "E-Mail oder markierten Text überarbeiten ..." in main_composer
    assert "E-Mail oder markierten Text überarbeiten ..." in global_composer
    assert "data-email-ai-preset-trigger" in main_composer
    assert "data-email-ai-preset-trigger" in global_composer
    assert "data-email-ai-preset-menu" in main_composer
    assert "data-email-ai-preset-instruction" in global_composer
    assert 'data-email-ai-accept aria-label="Änderung akzeptieren" title="Änderung akzeptieren"' in main_composer
    assert 'data-email-ai-reject aria-label="Änderung verwerfen" title="Änderung verwerfen"' in global_composer
    assert 'class="email-editor-ai-prompt-action-icon"' in main_composer
    assert 'class="email-editor-ai-prompt-action-icon"' in global_composer
    assert "data-email-ai-prompt-submit" in global_composer
    assert ".email-editor-ai-prompt {" in base_template
    assert "position: absolute;" in base_template
    assert "right: 50%;" in base_template
    assert "height: 264px !important;" in base_template
    assert "max-height: 264px !important;" in base_template
    assert "overflow: hidden;" in base_template
    assert "grid-template-columns: 1.1rem minmax(0, 1fr) var(--control-v1-height);" in base_template
    assert ".email-editor-ai-prompt.has-email-ai-suggestion {" in base_template
    assert "grid-template-columns: 1.1rem minmax(0, 1fr) auto var(--control-v1-height);" in base_template
    assert ".email-editor-ai-prompt button.email-editor-ai-prompt-action" in base_template
    assert "padding: 0; line-height: 1;" in base_template
    assert ".email-editor-ai-prompt-action-icon { display: block; width: 1.65rem; height: 1.65rem; }" in base_template
    assert ".email-editor-ai-prompt-submit svg { width: 1.65rem; height: 1.65rem;" in base_template
    assert ".email-editor-ai-prompt-actions[hidden] { display: none; }" in base_template
    assert ".email-editor-ai-prompt-submit:hover:not(:disabled)" in base_template
    assert "button.email-editor-ai-prompt-accept { border: 0;" in base_template
    assert "button.email-editor-ai-prompt-action[data-email-ai-reject] { border: 0;" in base_template
    assert "function constrainComposerEditor(content)" in base_template
    assert 'frame.setAttribute("scrolling", "auto")' in base_template
    assert 'documentElement.style.height = "100%"' in base_template
    assert 'body.style.margin = "0"' in base_template
    assert "layer.appendChild(prompt)" not in base_template
    assert "actions.hidden = !pending;" in base_template
    assert 'prompt.classList.toggle("has-email-ai-suggestion", pending);' in base_template
    assert "/emails/ai/rewrite" in base_template
    assert "data-email-ai-original" in base_template
    assert "is-email-ai-locked" in base_template
    assert ".is-email-ai-locked .email-rich-editor { pointer-events: none; }" not in base_template
    assert "#dff3e5" in base_template
    assert "var includesBlock" in base_template
    assert "var previewFragment = selected.range.cloneContents();" in base_template
    assert "function wholeEmailSelection(content)" in base_template
    assert "range.selectNodeContents(body);" in base_template
    assert "readSelection(content) || selectionByContent.get(content) || wholeEmailSelection(content)" in base_template
    assert "selected.isWholeEmail" in base_template
    assert "Die E-Mail enthält noch keinen Text, den die KI überarbeiten kann." in base_template
    assert "function setPresetMenuOpen(prompt, open)" in base_template
    assert "controls.input.value = option.dataset.emailAiPresetInstruction || \"\";" in base_template
    assert "setPresetMenuOpen(prompt, false);" in base_template
    assert "ohne Tabellen oder ganze Absätze" not in base_template


def test_mailbox_places_agent_action_directly_before_case_action_in_subject_row():
    reading_pane = Path("app/templates/emails_reading_pane.html").read_text(encoding="utf-8")
    mailbox_template = Path("app/templates/emails.html").read_text(encoding="utf-8")

    subject_actions = reading_pane.index('class="mailbox-reading-subject-actions"')
    agent_action = reading_pane.index("Mit Agent bearbeiten")
    case_action = reading_pane.index("Fall anlegen")

    assert subject_actions < agent_action < case_action
    assert reading_pane.index('class="mailbox-reading-actions"') > case_action
    assert ".mailbox-reading-subject-actions { display: flex;" in mailbox_template


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

        assert inbox.folder_counts == {"inbox": 2, "sent": 0, "drafts": 0, "unassigned": 1, "trash": 0, "spam": 0}
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

        rendered_pane = template.get_template("emails_reading_pane.html").render(
            selected=inbox.selected,
            folder="inbox",
            unread=False,
            csrf_token="test-token",
        )
        assert 'sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"' in rendered_pane

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
    assert "ix_customer_zoho_emails_direction_sent_at" in index_names
    assert "ix_customer_zoho_emails_mailbox_state_direction_sent_at" in index_names


def test_mailbox_separates_spam_and_trashed_messages_from_active_folders():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="Example GmbH", zoho_id="zoho-account-1")
        active_email = CustomerZohoEmail(
            customer=customer,
            zoho_message_id="active-email",
            source="zoho",
            direction="inbound",
            is_unread=True,
            encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Aktive Nachricht"})),
            zoho_sent_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
        )
        spam_email = CustomerZohoEmail(
            customer=customer,
            zoho_message_id="spam-email",
            source="zoho",
            direction="inbound",
            is_unread=True,
            mailbox_state="spam",
            encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Spam-Nachricht"})),
            zoho_sent_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
        )
        trashed_email = HubMailboxEmail(
            direction="inbound",
            is_unread=True,
            mailbox_state="trash",
            fingerprint="c" * 64,
            encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Gelöschte Nachricht"})),
            received_at=datetime(2026, 9, 3, 11, 0, tzinfo=UTC),
        )
        db.add_all([customer, active_email, spam_email, trashed_email])
        db.commit()

        service = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test")

        assert [message.subject for message in service.get_folder_view(folder="inbox", unread_only=False).messages] == ["Aktive Nachricht"]
        assert [message.subject for message in service.get_folder_view(folder="spam", unread_only=False).messages] == ["Spam-Nachricht"]
        assert [message.subject for message in service.get_folder_view(folder="trash", unread_only=False).messages] == ["Gelöschte Nachricht"]
        assert service.get_folder_counts() == {"inbox": 1, "sent": 0, "drafts": 0, "unassigned": 0, "trash": 1, "spam": 1}


def test_mailbox_batch_actions_update_all_linked_message_copies_and_unassigned_emails():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        first_customer = Customer(name="First GmbH", zoho_id="zoho-account-1")
        second_customer = Customer(name="Second GmbH", zoho_id="zoho-account-2")
        first_copy = CustomerZohoEmail(
            customer=first_customer,
            zoho_message_id="shared-message",
            source="zoho",
            direction="inbound",
            is_unread=True,
            encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Gemeinsame Nachricht"})),
        )
        second_copy = CustomerZohoEmail(
            customer=second_customer,
            zoho_message_id="shared-message",
            source="zoho",
            direction="inbound",
            is_unread=True,
            encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Gemeinsame Nachricht"})),
        )
        unassigned = HubMailboxEmail(
            direction="inbound",
            is_unread=True,
            fingerprint="d" * 64,
            encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Unbekannte Nachricht"})),
            received_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
        )
        db.add_all([first_customer, second_customer, first_copy, second_copy, unassigned])
        db.commit()

        service = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        first_copy_key = f"linked-{first_customer.id}-{first_copy.id}"
        unassigned_key = f"unassigned-{unassigned.id}"
        with pytest.raises(ValueError, match="Papierkorb"):
            service.apply_batch_action(keys=[first_copy_key], action="permanently_delete")
        assert service.apply_batch_action(
            keys=[first_copy_key],
            action="mark_read",
        ) == 2
        assert not first_copy.is_unread
        assert not second_copy.is_unread

        assert service.apply_batch_action(
            keys=[first_copy_key, unassigned_key],
            action="move_spam",
        ) == 3
        assert first_copy.mailbox_state == "spam"
        assert second_copy.mailbox_state == "spam"
        assert unassigned.mailbox_state == "spam"
        assert [message.subject for message in service.get_folder_view(folder="inbox", unread_only=False).messages] == []
        assert {message.subject for message in service.get_folder_view(folder="spam", unread_only=False).messages} == {
            "Unbekannte Nachricht",
            "Gemeinsame Nachricht",
        }
        assert service.apply_batch_action(
            keys=[first_copy_key, unassigned_key],
            action="restore",
        ) == 3
        assert first_copy.mailbox_state == "active"
        assert second_copy.mailbox_state == "active"
        assert unassigned.mailbox_state == "active"
        assert service.get_folder_view(folder="spam", unread_only=False).messages == ()

        assert service.apply_batch_action(
            keys=[first_copy_key],
            action="move_sent",
        ) == 2
        assert first_copy.direction == "outbound"
        assert second_copy.direction == "outbound"
        assert [message.subject for message in service.get_folder_view(folder="sent", unread_only=False).messages] == [
            "Gemeinsame Nachricht"
        ]

        assert service.apply_batch_action(
            keys=[unassigned_key],
            action="move_trash",
        ) == 1
        assert unassigned.mailbox_state == "trash"
        assert [message.subject for message in service.get_folder_view(folder="trash", unread_only=False).messages] == [
            "Unbekannte Nachricht"
        ]

        assert service.apply_batch_action(keys=[first_copy_key], action="move_trash") == 2
        assert service.apply_batch_action(
            keys=[first_copy_key, unassigned_key],
            action="permanently_delete",
        ) == 3
        assert db.get(CustomerZohoEmail, first_copy.id) is None
        assert db.get(CustomerZohoEmail, second_copy.id) is None
        assert db.get(HubMailboxEmail, unassigned.id) is None


def test_mailbox_drafts_are_encrypted_editable_and_separate_from_sent_emails():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        service = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        draft = service.save_draft(
            draft_id=None,
            sender_email="team@example.de",
            recipient_email="contact@example.de",
            recipient_key="contact:1",
            recipient_customer_id=7,
            recipient_name="Example Contact",
            subject="Noch nicht versendet",
            content="<p>Bearbeitbarer Entwurf</p>",
            cc_emails="cc@example.de",
            template_id="template-1",
            reply_to_email_id="12",
            forward_from_email_id="",
        )
        db.commit()

        drafts = service.get_view(folder="drafts", unread_only=False)
        assert drafts.folder_counts["drafts"] == 1
        assert [message.subject for message in drafts.messages] == ["Noch nicht versendet"]
        assert drafts.messages[0].kind == "draft"
        context = service.get_draft_compose_context(draft_id=draft.id)
        assert context["recipient_email"] == "contact@example.de"
        assert context["recipient"]["key"] == "contact:1"
        assert context["content"] == "<p>Bearbeitbarer Entwurf</p>"
        assert service.discard_draft(draft_id=draft.id)
        assert service.get_folder_counts()["drafts"] == 0


def test_mailbox_prepares_replies_and_forwards_for_unassigned_inbound_emails():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        inbound = HubMailboxEmail(
            source="mittwald-imap",
            direction="inbound",
            is_unread=True,
            fingerprint="r" * 64,
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "subject": "Rückfrage zur Website",
                        "from": {"name": "Example Contact", "email": "contact@example.de"},
                        "to": [{"email": "info@kosmos-medien.de"}],
                        "cc": [{"email": "team@example.de"}],
                        "content": "<p>Bitte um Rückmeldung.</p>",
                        "mittwald_message_id": "<original@example.de>",
                    }
                )
            ),
            received_at=datetime(2026, 9, 9, 8, 0, tzinfo=UTC),
        )
        db.add(inbound)
        db.flush()

        service = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        reply = service.get_unassigned_email_compose_context(email_id=inbound.id, action="reply_all")
        forward = service.get_unassigned_email_compose_context(email_id=inbound.id, action="forward")

        assert reply["recipient"] is None
        assert reply["recipient_email"] == "contact@example.de"
        assert reply["subject"] == "Re: Rückfrage zur Website"
        assert reply["cc_emails"] == ["info@kosmos-medien.de", "team@example.de"]
        assert reply["reply_to_email_id"] == inbound.id
        assert str(reply["content"]).startswith("<br><br>")
        assert "Bitte um Rückmeldung." in str(reply["content"])
        assert forward["recipient_email"] == ""
        assert forward["subject"] == "Fwd: Rückfrage zur Website"
        assert forward["forward_from_email_id"] == inbound.id
        assert "Weitergeleitete Nachricht" in str(forward["content"])

        threaded_message_id = service._unassigned_reply_message_id(
            email_id=inbound.id,
            recipient_email="contact@example.de",
        )
        assert threaded_message_id == "<original@example.de>"


def test_mailbox_forwards_stored_attachments_from_unassigned_inbound_emails(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    storage = EmailAttachmentStorage(root=tmp_path / "attachments", cipher=cipher, min_free_bytes=0)

    with Session(engine) as db:
        inbound = HubMailboxEmail(
            source="mittwald-imap",
            direction="inbound",
            is_unread=True,
            fingerprint="f" * 64,
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "subject": "Unterlagen",
                        "from": {"email": "contact@example.de"},
                        "attachments": [{"id": "source-attachment", "name": "unterlagen.pdf"}],
                    }
                )
            ),
            received_at=datetime(2026, 9, 9, 8, 0, tzinfo=UTC),
        )
        db.add(inbound)
        db.flush()
        db.add(
            HubMailboxAttachment(
                email_id=inbound.id,
                source="mittwald-imap",
                source_attachment_id="source-attachment",
                storage_key=storage.store(b"PDF-Inhalt"),
                content_type="application/pdf",
                byte_size=10,
                stored_at=datetime.now(UTC),
            )
        )
        db.flush()

        service = HubMailboxService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            attachment_storage=storage,
        )
        attachments = service._forwarded_unassigned_attachments(email_id=inbound.id)

        assert len(attachments) == 1
        assert attachments[0].filename == "unterlagen.pdf"
        assert attachments[0].content == b"PDF-Inhalt"
        assert attachments[0].content_type == "application/pdf"


def test_mailbox_sends_direct_email_via_mittwald_and_keeps_a_local_attachment(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    sent_messages = []

    class FakeSmtp:
        def __init__(self, *_args, **_kwargs):
            return None

        def login(self, _username, _password):
            return None

        def send_message(self, message, from_addr, to_addrs):
            sent_messages.append((message, from_addr, to_addrs))

        def quit(self):
            return None

    monkeypatch.setattr("app.services.hub_mailbox_transport.smtplib.SMTP_SSL", FakeSmtp)
    with Session(engine) as db:
        db.add(
            HubMailboxAccount(
                email_address="info@kosmos.example",
                display_name="Kosmos Hub",
                username="info@kosmos.example",
                encrypted_password=cipher.encrypt("mittwald-secret"),
                verified_at=datetime.now(UTC),
            )
        )
        db.commit()
        storage = EmailAttachmentStorage(root=tmp_path / "attachments", cipher=cipher, min_free_bytes=0)
        service = HubMailboxService(
            db=db,
            cipher=cipher,
            public_base_url="https://hub.example.test",
            attachment_storage=storage,
        )

        sent = service.send_direct_email(
            sender_email="info@kosmos.example",
            recipient_email="Example Contact <contact@example.de>",
            subject="Unterlagen",
            content="<p>Im Anhang.</p>",
            cc_emails="Buchhaltung <buchhaltung@example.de>",
            attachments=(
                CustomerCommunicationAttachmentUpload(
                    filename="unterlagen.txt",
                    content=b"Inhalt",
                    content_type="text/plain",
                ),
            ),
        )
        db.commit()

        sent_view = service.get_folder_view(folder="sent", unread_only=False, selected_key=f"unassigned-{sent.id}")
        assert sent_view.selected is not None
        assert sent_view.selected.kind == "direct"
        assert sent_view.selected.subject == "Unterlagen"
        assert sent_view.selected.cc_recipients == "Buchhaltung <buchhaltung@example.de>"
        assert service.get_folder_counts() == {"inbox": 0, "sent": 1, "drafts": 0, "unassigned": 0, "trash": 0, "spam": 0}
        attachment = db.scalar(select(HubMailboxAttachment).where(HubMailboxAttachment.email_id == sent.id))
        assert attachment is not None
        downloaded = service.download_unassigned_attachment(email_id=sent.id, attachment_id=attachment.source_attachment_id)
        assert downloaded.filename == "unterlagen.txt"
        assert downloaded.content == b"Inhalt"

    message, from_addr, to_addrs = sent_messages[0]
    assert from_addr == "info@kosmos.example"
    assert to_addrs == ["contact@example.de", "buchhaltung@example.de"]
    html_part = next(part for part in message.walk() if part.get_content_type() == "text/html")
    assert 'meta name="viewport" content="width=device-width, initial-scale=1"' in html_part.get_content()
    assert "Im Anhang." in html_part.get_content()
    assert any(part.get_filename() == "unterlagen.txt" for part in message.walk())


def test_mailbox_list_reads_compact_header_without_full_email_payload():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="Example GmbH", zoho_id="zoho-account-1")
        db.add(
            CustomerZohoEmail(
                customer=customer,
                zoho_message_id="zoho-email-1",
                source="zoho",
                direction="outbound",
                is_unread=False,
                encrypted_payload_json=cipher.encrypt(json.dumps({"content": "full email body only"})),
                encrypted_header_json=cipher.encrypt(
                    json.dumps(
                        {
                            "subject": "Kurz gespeichert",
                            "sender": "team@example.de",
                            "recipients": "customer@example.de",
                        }
                    )
                ),
                zoho_sent_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
            )
        )
        db.commit()

        service = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        message = service.get_folder_view(folder="sent", unread_only=False).messages[0]

        assert message.subject == "Kurz gespeichert"
        assert message.sender == "team@example.de"
        assert message.recipients == "customer@example.de"


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
