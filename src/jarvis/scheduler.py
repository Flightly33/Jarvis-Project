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
        self._announced: set[int] = set()
        self.calendar_lead_minutes = 15

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
        # calendar events that are about to start are announced once, like reminders
        if self.calendar_lead_minutes:
            for event in self._store.events(now, now + self.calendar_lead_minutes * 60):
                if event["all_day"] or event["id"] in self._announced:
                    continue
                self._announced.add(event["id"])
                clock = time.strftime("%H:%M", time.localtime(event["start"]))
                self._bus.emit("reminder", id=-event["id"], text=f"{event['title']} starts at {clock}", due_at=event["start"], late=False)
        return fired
