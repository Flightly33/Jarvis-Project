"""A small publish/subscribe bus that carries everything the interface shows.

Events are plain dicts with a `type` key. Subscribers each get their own
bounded queue; a slow subscriber loses its oldest events rather than stalling
the assistant.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

Event = dict[str, Any]

# Event types that are worth replaying to an interface that connects late.
_REPLAY_TYPES = {"user_message", "assistant_message", "tool_start", "tool_end", "notice", "reminder"}


class EventBus:
    def __init__(self, history: int = 200, queue_size: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: deque[Event] = deque(maxlen=history)
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop so worker threads can publish safely."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def history(self) -> list[Event]:
        return list(self._history)

    def emit(self, type: str, **data: Any) -> Event:
        """Publish from the event loop thread."""
        self._seq += 1
        event: Event = {"type": type, "seq": self._seq, "ts": time.time(), **data}
        if type in _REPLAY_TYPES:
            self._history.append(event)
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)
        return event

    def emit_threadsafe(self, type: str, **data: Any) -> None:
        """Publish from any thread (audio callbacks, worker threads)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: self.emit(type, **data))
