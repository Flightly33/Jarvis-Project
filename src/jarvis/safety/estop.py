"""The emergency stop: one call that halts everything the assistant is doing."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

Handler = Callable[[str], Awaitable[None] | None]


class EmergencyStop:
    """Runs every registered stop handler; one failing handler never blocks the others."""

    def __init__(self) -> None:
        self._handlers: list[tuple[str, Handler]] = []
        self.last_triggered: float | None = None
        self.last_reason: str = ""
        self.count = 0

    def register(self, name: str, handler: Handler) -> None:
        self._handlers.append((name, handler))

    async def trigger(self, reason: str = "requested") -> list[str]:
        """Stop everything. Returns the names of handlers that failed (normally empty)."""
        self.last_triggered = time.time()
        self.last_reason = reason
        self.count += 1
        failed: list[str] = []
        for name, handler in self._handlers:
            try:
                result = handler(reason)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=5.0)
            except Exception:  # noqa: BLE001 - a stop must never be prevented by one handler
                log.exception("emergency-stop handler %r failed", name)
                failed.append(name)
        return failed
