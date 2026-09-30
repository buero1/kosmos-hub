"""Wake the scheduled email sender only when a stored delivery is due."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.security import SecretCipher
from app.db.session import SessionLocal
from app.services.scheduled_emails import ScheduledEmailService

logger = logging.getLogger(__name__)
_RECONCILE_INTERVAL_SECONDS = 60.0


class ScheduledEmailWorker:
    """One in-process scheduler rebuilt from durable jobs after every restart."""

    _loop: asyncio.AbstractEventLoop | None = None
    _wake_event: asyncio.Event | None = None

    def __init__(self, *, settings: Settings, cipher: SecretCipher) -> None:
        self.settings = settings
        self.cipher = cipher

    @classmethod
    def notify_schedule_changed(cls) -> None:
        loop = cls._loop
        event = cls._wake_event
        if loop is None or event is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(event.set)

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        type(self)._loop = loop
        type(self)._wake_event = event
        try:
            while True:
                event.clear()
                await asyncio.to_thread(self._process_due)
                delay_seconds = await asyncio.to_thread(
                    self._seconds_until_next_delivery
                )
                if delay_seconds is None:
                    await event.wait()
                    continue
                with suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), timeout=delay_seconds)
        finally:
            if type(self)._loop is loop:
                type(self)._loop = None
                type(self)._wake_event = None

    def _process_due(self) -> None:
        with SessionLocal() as db:
            try:
                from app.services.lead_appointment_email_reminders import (
                    LeadAppointmentEmailReminderService,
                )

                result = LeadAppointmentEmailReminderService(
                    db=db,
                    cipher=self.cipher,
                    public_base_url=self.settings.public_base_url,
                ).reconcile()
                db.commit()
                if result.changed:
                    logger.info(
                        "Reconciled lead appointment emails: scheduled=%s updated=%s cancelled=%s blocked=%s",
                        result.scheduled,
                        result.updated,
                        result.cancelled,
                        result.blocked,
                    )
            except Exception:
                db.rollback()
                logger.exception("Lead appointment email reconciliation failed.")
            ScheduledEmailService(
                db=db,
                cipher=self.cipher,
                public_base_url=self.settings.public_base_url,
            ).process_due()

    def _seconds_until_next_delivery(self) -> float | None:
        with SessionLocal() as db:
            next_due_at = ScheduledEmailService(db=db, cipher=self.cipher).next_due_at()
        if next_due_at is None:
            return _RECONCILE_INTERVAL_SECONDS
        now = datetime.now(UTC).replace(tzinfo=None)
        return min(
            _RECONCILE_INTERVAL_SECONDS, max(0.0, (next_due_at - now).total_seconds())
        )
