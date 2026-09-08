from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_mailbox_account import HubMailboxAccount
from app.services.hub_mailbox_imap_import import HubMailboxImapImportService


def test_mittwald_message_parser_preserves_body_attachment_and_unread_state():
    cipher = SecretCipher("a" * 32)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    raw_message = b"\r\n".join(
        (
            b"From: Example <example@example.de>",
            b"To: Team <team@kosmos.example>",
            b"Subject: Anfrage mit Anhang",
            b"Date: Sat, 05 Sep 2026 10:30:00 +0200",
            b"Message-ID: <mail-123@example.de>",
            b"MIME-Version: 1.0",
            b'Content-Type: multipart/mixed; boundary="mail-boundary"',
            b"",
            b"--mail-boundary",
            b'Content-Type: text/html; charset="utf-8"',
            b"",
            b"<p>Hallo Team</p>",
            b"--mail-boundary",
            b'Content-Type: text/plain; name="angebot.txt"',
            b"Content-Disposition: attachment; filename=angebot.txt",
            b"Content-Transfer-Encoding: base64",
            b"",
            b"VGVzdC1Bbmhhbmc=",
            b"--mail-boundary--",
            b"",
        )
    )

    with Session(engine) as db:
        service = HubMailboxImapImportService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        account = HubMailboxAccount(id=1, email_address="info@kosmos.example", encrypted_password=cipher.encrypt("secret"))
        parsed = service._parse_message(
            raw_message=raw_message,
            account=account,
            folder="INBOX",
            uid="42",
            flags="",
        )

        assert parsed.identity == "<mail-123@example.de>"
        assert parsed.is_unread is True
        assert parsed.occurred_at == datetime(2026, 9, 5, 8, 30, tzinfo=UTC)
        assert parsed.payload["content"] == "<p>Hallo Team</p>"
        assert parsed.payload["from"] == [{"name": "Example", "email": "example@example.de"}]
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].filename == "angebot.txt"
        assert parsed.attachments[0].content == b"Test-Anhang"


def test_mittwald_message_is_deduplicated_before_a_second_store():
    cipher = SecretCipher("a" * 32)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubMailboxImapImportService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        account = HubMailboxAccount(id=1, email_address="info@kosmos.example", encrypted_password=cipher.encrypt("secret"))
        parsed = service._parse_message(
            raw_message=b"From: sender@example.de\r\nTo: team@kosmos.example\r\nSubject: Test\r\nMessage-ID: <once@example.de>\r\n\r\nText",
            account=account,
            folder="INBOX",
            uid="77",
            flags="\\Seen",
        )

        assert service._store_message(parsed=parsed)[0] == "imported"
        db.commit()
        assert service._store_message(parsed=parsed)[0] == "skipped"
