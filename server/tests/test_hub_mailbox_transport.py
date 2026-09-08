from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_mailbox_account import HubMailboxAccount
from app.services.hub_mailbox_transport import (
    HubMailboxTransportAttachment,
    HubMailboxTransportInlineImage,
    HubMailboxTransportService,
)


def test_mittwald_smtp_delivery_uses_the_configured_sender_and_builds_a_mime_message(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    sent_messages = []

    class FakeSmtp:
        def __init__(self, *_args, **_kwargs):
            self.logged_in = None

        def login(self, username, password):
            self.logged_in = (username, password)

        def send_message(self, message, from_addr, to_addrs):
            sent_messages.append((message, from_addr, to_addrs, self.logged_in))

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

        delivery = HubMailboxTransportService(db=db, cipher=cipher).send(
            sender_email="info@kosmos.example",
            recipient_name="Example GmbH",
            recipient_email="kontakt@example.de",
            subject="Anfrage",
            html_content='<p>Hallo</p><img src="cid:hub-image-1@kosmos-hub">',
            cc_recipients=(("Buchhaltung", "buchhaltung@example.de"),),
            reply_to_message_id="<parent@example.de>",
            attachments=(
                HubMailboxTransportAttachment(
                    filename="angebot.txt",
                    content=b"Angebot",
                    content_type="text/plain",
                ),
            ),
            inline_images=(
                HubMailboxTransportInlineImage(
                    content_id="hub-image-1@kosmos-hub",
                    filename="logo.png",
                    content=b"\x89PNG\r\n\x1a\nimage",
                    content_type="image/png",
                ),
            ),
        )

    assert delivery.message_id.startswith("<")
    message, from_addr, to_addrs, login = sent_messages[0]
    assert from_addr == "info@kosmos.example"
    assert to_addrs == ["kontakt@example.de", "buchhaltung@example.de"]
    assert login == ("info@kosmos.example", "mittwald-secret")
    assert message["In-Reply-To"] == "<parent@example.de>"
    assert message.get_body(preferencelist=("html",)).get_content().strip() == '<p>Hallo</p><img src="cid:hub-image-1@kosmos-hub">'
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "Hallo\n[Bild]"
    assert any(part.get_filename() == "angebot.txt" for part in message.walk())
    assert any(part.get("Content-ID") == "<hub-image-1@kosmos-hub>" for part in message.walk())


def test_list_senders_prioritizes_the_standard_info_mailbox():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        db.add_all((
            HubMailboxAccount(
                email_address="buchhaltung@kosmos-medien.de",
                display_name="Buchhaltung",
                username="buchhaltung@kosmos-medien.de",
                encrypted_password=cipher.encrypt("secret"),
                verified_at=datetime.now(UTC),
            ),
            HubMailboxAccount(
                email_address="info@kosmos-medien.de",
                display_name="Kosmos Medien",
                username="info@kosmos-medien.de",
                encrypted_password=cipher.encrypt("secret"),
                verified_at=datetime.now(UTC),
            ),
        ))
        db.commit()

        senders = HubMailboxTransportService(db=db, cipher=cipher).list_senders()

    assert [sender.email for sender in senders] == [
        "info@kosmos-medien.de",
        "buchhaltung@kosmos-medien.de",
    ]


def test_plain_text_alternative_keeps_only_visible_body_content():
    html_content = """<!doctype html><html><head><style>
    html, body { margin: 0; }
    </style><title>Nicht sichtbar</title></head><body>
    <!-- Compiler-Kommentar --><p>Hallo<br>Welt</p>
    <table><tr><td>Links</td><td>Rechts</td></tr></table>
    </body></html>"""

    plain_text = HubMailboxTransportService._plain_text(html_content)

    assert plain_text == "Hallo\nWelt\nLinks Rechts"
    assert "html, body" not in plain_text
    assert "Nicht sichtbar" not in plain_text
    assert "Compiler-Kommentar" not in plain_text
