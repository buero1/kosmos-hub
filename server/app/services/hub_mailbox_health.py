"""Health monitoring for the automatic Mittwald INBOX synchronization."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.models.hub_mailbox_imap_sync_failure import HubMailboxImapSyncFailure
from app.models.hub_user import HubUser
from app.services.hub_mailbox import HubMailboxService, MAILBOX_HEALTH_ALERT_SOURCE


logger = logging.getLogger(__name__)
_INBOX_FOLDER = "INBOX"
_FAILURE_THRESHOLD = 3
_SUCCESS_TIMEOUT = timedelta(minutes=5)
_PREFERRED_SENDER = "info@kosmos-medien.de"


@dataclass(frozen=True)
class HubMailboxHealthSummary:
    alerts_created: int = 0
    emails_sent: int = 0


class HubMailboxHealthService:
    """Create one durable alert per INBOX outage and notify configured admins."""

    def __init__(self, *, db: Session, cipher: SecretCipher, public_base_url: str) -> None:
        self.db = db
        self.cipher = cipher
        self.public_base_url = public_base_url.rstrip("/")

    def notify_unhealthy_inboxes(self) -> HubMailboxHealthSummary:
        now = datetime.now(UTC)
        states = self.db.scalars(
            select(HubMailboxImapSyncState)
            .join(HubMailboxAccount)
            .where(
                HubMailboxImapSyncState.folder == _INBOX_FOLDER,
                HubMailboxAccount.enabled.is_(True),
                HubMailboxAccount.verified_at.is_not(None),
            )
            .order_by(HubMailboxAccount.email_address.asc())
        ).all()
        alerts_created = emails_sent = 0
        for state in states:
            if state.alerted_at is not None:
                continue
            reason = self._alert_reason(state=state, now=now)
            if reason is None:
                continue

            sent = self._send_alert(state=state, reason=reason)
            if sent:
                state.alerted_at = now
                self.db.commit()
                alerts_created += 1
                emails_sent += sent

        failures = self.db.scalars(
            select(HubMailboxImapSyncFailure)
            .where(
                HubMailboxImapSyncFailure.resolved_at.is_(None),
                HubMailboxImapSyncFailure.alerted_at.is_(None),
                HubMailboxImapSyncFailure.folder == _INBOX_FOLDER)
            .order_by(HubMailboxImapSyncFailure.id.asc())
            .limit(20)
        ).all()
        states_by_account = {state.mailbox_account_id: state for state in states}
        for failure in failures:
            state = states_by_account.get(failure.mailbox_account_id)
            if state is None:
                continue
            sent = self._send_alert(
                state=state,
                reason=f"Nachricht UID {failure.imap_uid} konnte nicht übernommen werden; spätere Nachrichten werden weiter abgerufen",
                error=failure.error,
            )
            if sent:
                failure.alerted_at = now
                self.db.commit()
                alerts_created += 1
                emails_sent += sent
        return HubMailboxHealthSummary(alerts_created=alerts_created, emails_sent=emails_sent)

    @staticmethod
    def _alert_reason(*, state: HubMailboxImapSyncState, now: datetime) -> str | None:
        if state.consecutive_failures >= _FAILURE_THRESHOLD:
            return f"{state.consecutive_failures} aufeinanderfolgende INBOX-Abgleichfehler"
        reference = state.last_success_at or state.created_at
        if reference is not None and HubMailboxHealthService._as_utc(reference) <= now - _SUCCESS_TIMEOUT:
            return "seit mindestens 5 Minuten kein erfolgreicher INBOX-Abgleich"
        return None

    def _send_alert(self, *, state: HubMailboxImapSyncState, reason: str, error: str | None = None) -> int:
        account = self.db.get(HubMailboxAccount, state.mailbox_account_id)
        if account is None:
            return 0
        recipients = self._recipient_emails()
        sender_email = self._sender_email()
        if not recipients or sender_email is None:
            logger.warning(
                "Mittwald INBOX alert for %s could not be emailed: sender or independent alert recipient missing.",
                account.email_address,
            )
            return 0

        content = self._alert_html(account=account, state=state, reason=reason, error=error)
        sent = 0
        for recipient in recipients:
            try:
                HubMailboxService(
                    db=self.db,
                    cipher=self.cipher,
                    public_base_url=self.public_base_url,
                ).send_direct_email(
                    sender_email=sender_email,
                    recipient_email=recipient,
                    subject=f"Hub-Warnung: INBOX-Abruf {account.email_address}",
                    content=content,
                    cc_emails="",
                    source=MAILBOX_HEALTH_ALERT_SOURCE,
                )
                self.db.commit()
                sent += 1
            except Exception:
                self.db.rollback()
                logger.exception("Mittwald INBOX alert email could not be sent to %s.", recipient)
        return sent

    def _recipient_emails(self) -> tuple[str, ...]:
        addresses = self.db.scalars(
            select(HubUser.mailbox_alert_email)
            .where(
                HubUser.is_active.is_(True),
                HubUser.role == "admin",
                HubUser.mailbox_alert_email.is_not(None),
            )
            .order_by(HubUser.id.asc())
        ).all()
        monitored = {address.casefold() for address in self.db.scalars(select(HubMailboxAccount.email_address)).all()}
        return tuple(dict.fromkeys(
            address.strip().casefold() for address in addresses
            if address and address.strip() and address.strip().casefold() not in monitored
        ))

    def _sender_email(self) -> str | None:
        accounts = self.db.scalars(
            select(HubMailboxAccount)
            .where(HubMailboxAccount.enabled.is_(True), HubMailboxAccount.verified_at.is_not(None))
            .order_by(HubMailboxAccount.email_address.asc())
        ).all()
        preferred = next((account for account in accounts if account.email_address.casefold() == _PREFERRED_SENDER), None)
        return (preferred or (accounts[0] if accounts else None)).email_address if accounts else None

    def _alert_html(
        self, *, account: HubMailboxAccount, state: HubMailboxImapSyncState,
        reason: str, error: str | None = None,
    ) -> str:
        last_success = self._format_timestamp(state.last_success_at)
        last_error = html.escape(error or state.last_error or "Keine Detailmeldung verfügbar.")
        return (
            "<p>Der automatische Abruf des Mittwald-Posteingangs benötigt Aufmerksamkeit.</p>"
            "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\">"
            f"<tr><td><strong>Postfach</strong></td><td>&nbsp;{html.escape(account.email_address)}</td></tr>"
            f"<tr><td><strong>Grund</strong></td><td>&nbsp;{html.escape(reason)}</td></tr>"
            f"<tr><td><strong>Letzter Erfolg</strong></td><td>&nbsp;{html.escape(last_success)}</td></tr>"
            f"<tr><td><strong>Letzte Meldung</strong></td><td>&nbsp;{last_error}</td></tr>"
            "</table>"
            f"<p>Details und Zugangsdaten: <a href=\"{html.escape(self.public_base_url + '/account#account-mailbox', quote=True)}\" "
            "target=\"_blank\">E-Mail-Postfächer im Hub öffnen</a>.</p>"
        )

    @staticmethod
    def _format_timestamp(value: datetime | None) -> str:
        if value is None:
            return "Noch kein erfolgreicher Abruf"
        return HubMailboxHealthService._as_utc(value).strftime("%d.%m.%Y %H:%M UTC")

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
