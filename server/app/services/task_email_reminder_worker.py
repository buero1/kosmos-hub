"""Wake the task reminder sender only when a stored delivery is due."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.security import SecretCipher
from app.db.session import SessionLocal
from app.services.task_email_reminders import TaskEmailReminderService


class TaskEmailReminderWorker:
    """One in-process scheduler that is rebuilt from durable jobs after a restart."""

    _loop: asyncio.AbstractEventLoop | None = None
    _wake_event: asyncio.Event | None = None

    def __init__(self, *, settings: Settings, cipher: SecretCipher) -> None:
        self.settings = settings
        self.cipher = cipher

    @classmethod
    def notify_schedule_changed(cls) -> None:
        """Wake the worker after a task is created, changed, or deleted."""
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
                # Clear before querying so an update during the query remains a wake signal.
                event.clear()
                await asyncio.to_thread(self._process_due_reminders)
                delay_seconds = await asyncio.to_thread(self._seconds_until_next_delivery)
                if delay_seconds is None:
                    await event.wait()
                    continue
                try:
                    await asyncio.wait_for(event.wait(), timeout=delay_seconds)
                except TimeoutError:
                    pass
        finally:
            if type(self)._loop is loop:
                type(self)._loop = None
                type(self)._wake_event = None

    def _process_due_reminders(self) -> None:
        with SessionLocal() as db:
            TaskEmailReminderService(
                db=db,
                cipher=self.cipher,
                public_base_url=self.settings.public_base_url,
            ).process_due_reminders()

    def _seconds_until_next_delivery(self) -> float | None:
        with SessionLocal() as db:
            next_due_at = TaskEmailReminderService(db=db).next_due_at()
        if next_due_at is None:
            return None
        now = datetime.now(UTC).replace(tzinfo=None)
        return max(0.0, (next_due_at - now).total_seconds())
