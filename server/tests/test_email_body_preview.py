import json
from html.parser import HTMLParser

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_mailbox_imap_import import HubMailboxImapImportService


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ""

    def handle_data(self, data):
        self.text += data


@pytest.mark.parametrize("content", [
    "Delivery failed:\n<recipient@example.de>: 552 Quota exceeded.",
    "From: Name <first.last+tag@example.org>\nTo: <other@example.net>",
    "<UPPER@EXAMPLE.DE>: mailbox full",
    "See <https://example.org/report?a=1&b=2>",
    "Values: 2 < 3 & 4 > 1",
    "Text with &lt;literal entity&gt;",
])
def test_legacy_plain_text_keeps_addresses_brackets_entities_and_line_breaks(content):
    preview = CustomerCommunicationService._email_preview_document(content)
    visible = VisibleText()
    visible.feed(preview)
    assert visible.text == content
    assert "<pre>" in preview
    assert "default-src 'none'" in preview


@pytest.mark.parametrize("content_type", ["text/plain", "TEXT/PLAIN; charset=UTF-8"])
def test_explicit_plain_text_never_turns_literal_markup_into_html(content_type):
    content = '<b>literal</b> <recipient@example.de>\n<img src="https://example.org/image.png"><script>alert(1)</script>'
    preview = CustomerCommunicationService._email_preview_document(
        content, content_type=content_type, image_url_prefix="/image-cache",
    )
    visible = VisibleText()
    visible.feed(preview)
    assert visible.text == content
    assert "<script>" not in preview and "<img " not in preview
    assert "/image-cache" not in preview


@pytest.mark.parametrize("content_type", [None, "text/html", "TEXT/HTML; charset=UTF-8"])
@pytest.mark.parametrize("content", [
    '<p>Hello <strong>Customer</strong></p><br/>',
    '<table style="padding: 10px"><tr><td><a href="mailto:person@example.de">Mail</a></td></tr></table>',
    '<html><head><style>p { color: red; }</style></head><body><p>Hello</p></body></html>',
    '<o:p>Outlook text</o:p>',
])
def test_real_html_keeps_its_formatting_and_preview_security(content_type, content):
    preview = CustomerCommunicationService._email_preview_document(content, content_type=content_type)
    assert content in preview
    assert "<pre>" not in preview
    assert "default-src 'none'" in preview
    assert "form-action 'none'" in preview


def test_empty_and_missing_bodies_remain_supported():
    assert CustomerCommunicationService._email_preview_document(None) is None
    assert "<pre></pre>" in CustomerCommunicationService._email_preview_document("")


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("content_type", ["text/plain", "text/html"])
def test_imap_format_survives_storage_and_reaches_mailbox_and_customer_preview(linked, content_type):
    cipher = SecretCipher("a" * 32)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    content = '<p>Message</p> &lt;recipient@example.de&gt;'
    if content_type == "text/plain":
        content = '<p>Literal text</p>\n<recipient@example.de>: 552 Quota exceeded.'
    raw = ("From: sender@example.de\r\nTo: team@example.de\r\nSubject: Preview test\r\n"
           "Message-ID: <preview-test@example.de>\r\nMIME-Version: 1.0\r\n"
           f"Content-Type: {content_type}; charset=utf-8\r\n\r\n{content}").encode()
    with Session(engine) as db:
        if linked:
            db.add(Customer(name="Test customer", encrypted_profile_json=cipher.encrypt(json.dumps({
                "fields": {"Kontakt-E-Mail": "sender@example.de"},
            }))))
            db.flush()
        importer = HubMailboxImapImportService(db=db, cipher=cipher, public_base_url="https://hub.example")
        account = HubMailboxAccount(id=1, email_address="team@example.de", encrypted_password=cipher.encrypt("unused"))
        parsed = importer._parse_message(raw_message=raw, account=account, folder="INBOX", uid="1", flags="")
        assert parsed.payload["content"] == content
        assert parsed.payload["content_type"] == content_type
        assert importer._store_message(parsed=parsed)[0] == "imported"
        db.commit()
        if linked:
            email = db.scalar(select(CustomerZohoEmail))
            preview = importer.communications._email_view(email).preview_html
        else:
            email = db.scalar(select(HubMailboxEmail))
            mailbox = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example")
            preview = mailbox._unassigned_message(email).preview_html
        assert json.loads(cipher.decrypt(email.encrypted_payload_json))["content_type"] == content_type
        if content_type == "text/plain":
            visible = VisibleText()
            visible.feed(preview)
            assert visible.text == content
            assert "<p>Literal text</p>" not in preview
        else:
            assert content in preview
            assert "<pre>" not in preview
    engine.dispose()
