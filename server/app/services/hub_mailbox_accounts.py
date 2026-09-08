"""Secure Mittwald mailbox setup and connection checks for the Hub mail client."""

from __future__ import annotations

import imaplib
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.models.hub_user import HubUser


MITTWALD_MAIL_HOST = "mail.agenturserver.de"
MITTWALD_IMAP_SSL_PORT = 993
MITTWALD_SMTP_SSL_PORT = 465
MITTWALD_IMAP_ROOT_FOLDER = "INBOX"
_CONNECTION_TIMEOUT_SECONDS = 15


class HubMailboxAccountError(ValueError):
    """A safe message for a mailbox configuration or connectivity failure."""


@dataclass(frozen=True)
class HubMailboxAccountStatus:
    id: int
    email_address: str
    display_name: str
    username: str
    imap_host: str
    imap_port: int
    imap_root_folder: str
    smtp_host: str
    smtp_port: int
    enabled: bool
    verified_at: datetime | None
    last_tested_at: datetime | None
    last_error: str | None
    inbox_last_success_at: datetime | None
    inbox_consecutive_failures: int
    inbox_last_error: str | None
    inbox_alerted_at: datetime | None
    sent_last_success_at: datetime | None
    sent_last_error: str | None


class HubMailboxAccountService:
    """Manage account credentials without exposing them after form submission."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        connection_tester: Callable[[str, str], None] | None = None,
    ) -> None:
        self.db = db
        self.cipher = cipher
        self._connection_tester = connection_tester or self._test_mittwald_connection

    def list_statuses(self) -> tuple[HubMailboxAccountStatus, ...]:
        accounts = self.db.scalars(select(HubMailboxAccount).order_by(HubMailboxAccount.email_address.asc())).all()
        states = self.db.scalars(
            select(HubMailboxImapSyncState).where(
                HubMailboxImapSyncState.mailbox_account_id.in_(tuple(account.id for account in accounts))
            )
        ).all() if accounts else ()
        states_by_account_folder = {(state.mailbox_account_id, state.folder): state for state in states}
        return tuple(self._status(account, states_by_account_folder) for account in accounts)

    def test_and_save(
        self,
        *,
        email_address: str,
        display_name: str,
        username: str,
        password: str,
        configured_by: HubUser,
    ) -> HubMailboxAccount:
        address = self._email_address(email_address)
        name = display_name.strip()[:160] or address
        mailbox_username = username.strip()[:320] or address
        existing = self.db.scalar(select(HubMailboxAccount).where(HubMailboxAccount.email_address == address))
        secret = password if password else (self.cipher.decrypt(existing.encrypted_password) if existing is not None else "")
        if not secret:
            raise HubMailboxAccountError("Bitte das Passwort des Mittwald-Postfachs eingeben.")
        if len(secret) > 1_024:
            raise HubMailboxAccountError("Das Postfach-Passwort ist zu lang.")

        try:
            self._connection_tester(mailbox_username, secret)
        except HubMailboxAccountError as exc:
            if existing is not None:
                existing.last_tested_at = datetime.now(UTC)
                existing.last_error = str(exc)[:255]
                self.db.flush()
            raise

        now = datetime.now(UTC)
        if existing is None:
            account = HubMailboxAccount(
                email_address=address,
                display_name=name,
                username=mailbox_username,
                encrypted_password=self.cipher.encrypt(secret),
                imap_host=MITTWALD_MAIL_HOST,
                imap_port=MITTWALD_IMAP_SSL_PORT,
                imap_root_folder=MITTWALD_IMAP_ROOT_FOLDER,
                smtp_host=MITTWALD_MAIL_HOST,
                smtp_port=MITTWALD_SMTP_SSL_PORT,
                configured_by_user_id=configured_by.id,
            )
            self.db.add(account)
        else:
            account = existing
            account.display_name = name
            account.username = mailbox_username
            account.imap_host = MITTWALD_MAIL_HOST
            account.imap_port = MITTWALD_IMAP_SSL_PORT
            account.imap_root_folder = MITTWALD_IMAP_ROOT_FOLDER
            account.smtp_host = MITTWALD_MAIL_HOST
            account.smtp_port = MITTWALD_SMTP_SSL_PORT
            account.enabled = True
            account.configured_by_user_id = configured_by.id
            if password:
                account.encrypted_password = self.cipher.encrypt(secret)
        account.verified_at = now
        account.last_tested_at = now
        account.last_error = None
        self.db.flush()
        return account

    @staticmethod
    def _email_address(value: str) -> str:
        address = value.strip().casefold()
        _, parsed = parseaddr(address)
        if not parsed or parsed.casefold() != address or "@" not in address or len(address) > 320:
            raise HubMailboxAccountError("Bitte eine gültige E-Mail-Adresse eingeben.")
        return address

    @staticmethod
    def _test_mittwald_connection(username: str, password: str) -> None:
        context = ssl.create_default_context()
        imap = None
        smtp = None
        try:
            imap = imaplib.IMAP4_SSL(
                MITTWALD_MAIL_HOST,
                MITTWALD_IMAP_SSL_PORT,
                ssl_context=context,
                timeout=_CONNECTION_TIMEOUT_SECONDS,
            )
            imap.login(username, password)
            smtp = smtplib.SMTP_SSL(
                MITTWALD_MAIL_HOST,
                MITTWALD_SMTP_SSL_PORT,
                context=context,
                timeout=_CONNECTION_TIMEOUT_SECONDS,
            )
            smtp.login(username, password)
        except imaplib.IMAP4.error as exc:
            raise HubMailboxAccountError("Die IMAP-Anmeldung bei Mittwald wurde abgelehnt. Benutzername und Passwort prüfen.") from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise HubMailboxAccountError("Die SMTP-Anmeldung bei Mittwald wurde abgelehnt. Benutzername und Passwort prüfen.") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise HubMailboxAccountError("Die Mittwald-Mailserver sind momentan nicht erreichbar. Bitte die Verbindung später erneut testen.") from exc
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except (OSError, imaplib.IMAP4.error):
                    pass
            if smtp is not None:
                try:
                    smtp.quit()
                except (OSError, smtplib.SMTPException):
                    pass

    @staticmethod
    def _status(
        account: HubMailboxAccount,
        states_by_account_folder: dict[tuple[int, str], HubMailboxImapSyncState],
    ) -> HubMailboxAccountStatus:
        inbox = states_by_account_folder.get((account.id, "INBOX"))
        sent = states_by_account_folder.get((account.id, "INBOX.Sent"))
        return HubMailboxAccountStatus(
            id=account.id,
            email_address=account.email_address,
            display_name=account.display_name,
            username=account.username,
            imap_host=account.imap_host,
            imap_port=account.imap_port,
            imap_root_folder=account.imap_root_folder,
            smtp_host=account.smtp_host,
            smtp_port=account.smtp_port,
            enabled=account.enabled,
            verified_at=account.verified_at,
            last_tested_at=account.last_tested_at,
            last_error=account.last_error,
            inbox_last_success_at=inbox.last_success_at if inbox else None,
            inbox_consecutive_failures=inbox.consecutive_failures if inbox else 0,
            inbox_last_error=inbox.last_error if inbox else None,
            inbox_alerted_at=inbox.alerted_at if inbox else None,
            sent_last_success_at=sent.last_success_at if sent else None,
            sent_last_error=sent.last_error if sent else None,
        )
