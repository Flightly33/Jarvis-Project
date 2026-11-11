"""Fires reminders when they come due, including ones that came due while Jarvis was not running."""

from __future__ import annotations

import asyncio
import contextlib
import time

from jarvis.events import EventBus
from jarvis.memory import Reminder, Store


class ReminderScheduler:
    def __init__(self, store: Store, bus: EventBus, poll_seconds: float = 5.0) -> None:
        self._store = store
        self._bus = bus
        self._poll = poll_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(self._poll)

    def tick(self, now: float | None = None) -> list[Reminder]:
        """Announce every reminder that is due. Returns the ones that fired."""
        now = time.time() if now is None else now
        fired = []
        for reminder in self._store.due_reminders(now):
            if self._store.set_reminder_status(reminder.id, "fired"):     # guards against announcing twice
                fired.append(reminder)
                self._bus.emit("reminder", id=reminder.id, text=reminder.text, due_at=reminder.due_at, late=now - reminder.due_at > 120)
        return fired
