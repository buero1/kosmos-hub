from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_user import HubUser
from app.services.hub_mailbox_accounts import HubMailboxAccountError, HubMailboxAccountService


def test_mittwald_mailbox_account_is_verified_and_password_is_encrypted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls: list[tuple[str, str]] = []

    with Session(engine) as db:
        user = HubUser(username="admin", password_hash="hash", role="admin")
        db.add(user)
        db.flush()
        cipher = SecretCipher("a" * 32)
        service = HubMailboxAccountService(
            db=db,
            cipher=cipher,
            connection_tester=lambda username, password: calls.append((username, password)),
        )

        account = service.test_and_save(
            email_address="Info@Kosmos-Medien.de",
            display_name="Kosmos Medien",
            username="mailbox-123",
            password="mittwald-secret",
            configured_by=user,
        )
        db.commit()

        stored = db.get(HubMailboxAccount, account.id)
        assert stored is not None
        assert calls == [("mailbox-123", "mittwald-secret")]
        assert stored.email_address == "info@kosmos-medien.de"
        assert stored.encrypted_password != "mittwald-secret"
        assert cipher.decrypt(stored.encrypted_password) == "mittwald-secret"
        assert stored.imap_host == "mail.agenturserver.de"
        assert stored.imap_port == 993
        assert stored.smtp_port == 465
        assert service.list_statuses()[0].last_error is None
        assert service.list_statuses()[0].verified_at is not None


def test_failed_mittwald_test_keeps_existing_credentials_and_records_safe_error():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="admin", password_hash="hash", role="admin")
        db.add(user)
        db.flush()
        cipher = SecretCipher("a" * 32)
        working = HubMailboxAccountService(db=db, cipher=cipher, connection_tester=lambda _username, _password: None)
        account = working.test_and_save(
            email_address="info@kosmos-medien.de",
            display_name="Kosmos Medien",
            username="info@kosmos-medien.de",
            password="working-secret",
            configured_by=user,
        )
        db.commit()

        def reject(_username: str, _password: str) -> None:
            raise HubMailboxAccountError("Die IMAP-Anmeldung bei Mittwald wurde abgelehnt. Benutzername und Passwort prüfen.")

        failing = HubMailboxAccountService(db=db, cipher=cipher, connection_tester=reject)
        try:
            failing.test_and_save(
                email_address="info@kosmos-medien.de",
                display_name="Nicht speichern",
                username="incorrect-user",
                password="incorrect-secret",
                configured_by=user,
            )
        except HubMailboxAccountError as exc:
            assert "IMAP-Anmeldung" in str(exc)
        else:
            raise AssertionError("A rejected mailbox connection must not be saved.")

        stored = db.get(HubMailboxAccount, account.id)
        assert stored is not None
        assert stored.display_name == "Kosmos Medien"
        assert stored.username == "info@kosmos-medien.de"
        assert cipher.decrypt(stored.encrypted_password) == "working-secret"
        assert stored.last_error is not None
