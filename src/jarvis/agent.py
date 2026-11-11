"""The conversation manager that sits between the interfaces and a reasoning backend.

It runs one request at a time, attaches the per-request context, keeps the
transcript, starts a fresh conversation after a long silence (which keeps
usage low) and makes interruption reliable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable

from jarvis.brain.base import AgentBackend, TurnResult
from jarvis.persona import IGNORE_SENTINEL, context_block
from jarvis.safety import ConversationFlags
from jarvis.tools import Services, TurnState

log = logging.getLogger(__name__)


class _ReplyFilter:
    """Passes reply text on, unless the reply turns out to be the 'not addressed to me' marker."""

    def __init__(self, sink: Callable[[str], None], watch: bool) -> None:
        self._sink = sink
        self._held = "" if watch else None
        self.ignored = False

    def __call__(self, text: str) -> None:
        if self._held is None:
            self._sink(text)
            return
        self._held += text
        probe = self._held.lstrip()
        if IGNORE_SENTINEL.startswith(probe) and len(probe) < len(IGNORE_SENTINEL):
            return                              # still could be the marker; keep holding
        if probe.startswith(IGNORE_SENTINEL):
            self.ignored = True
            return
        held, self._held = self._held, None     # ordinary reply: release what was held
        self._sink(held)


class Assistant:
    def __init__(self, services: Services, backend: AgentBackend) -> None:
        self.services = services
        self.backend = backend
        self.flags = ConversationFlags()
        self.conversation_id = uuid.uuid4().hex[:12]
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[TurnResult] | None = None
        self._last_activity = 0.0
        self._fresh = True
        self._interrupting = False

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def new_conversation(self) -> None:
        await self.backend.reset()
        self.flags.reset()
        self.conversation_id = uuid.uuid4().hex[:12]
        self._fresh = True
        self.services.bus.emit("conversation", id=self.conversation_id)

    async def ask(self, text: str, source: str = "text", on_text: Callable[[str], None] | None = None) -> TurnResult:
        """Answer one request. Returns when the reply is complete, failed or was interrupted."""
        text = text.strip()
        if not text:
            return TurnResult()
        bus, store = self.services.bus, self.services.store
        async with self._lock:
            idle_limit = self.services.config.brain.session_idle_minutes * 60
            if not self._fresh and time.time() - self._last_activity > idle_limit:
                await self.new_conversation()

            recap = store.recap(self.conversation_id) if self._fresh else ""
            prompt = f"{text}\n\n{context_block(source, recap=recap)}"
            turn = TurnState(source=source, conversation_id=self.conversation_id, flags=self.flags)
            if source != "system":
                bus.emit("user_message", text=text, source=source)
                store.add_transcript(self.conversation_id, "user", text, {"source": source})
            bus.emit("state", processing=True)

            def forward(delta: str) -> None:
                bus.emit("assistant_delta", text=delta)
                if on_text:
                    on_text(delta)

            reply = _ReplyFilter(forward, watch=source == "voice-followup")
            self._interrupting = False
            self._task = asyncio.create_task(self.backend.run_turn(prompt, turn, reply))
            try:
                result = await asyncio.shield(self._task)
            except asyncio.CancelledError:
                if self._interrupting and self._task.cancelled():
                    result = TurnResult(interrupted=True)       # we stopped it ourselves
                else:
                    self._task.cancel()                         # the caller went away: stop the work too
                    raise
            except Exception as exc:  # noqa: BLE001 - a backend bug must surface as an error, not kill the app
                log.exception("backend failure")
                result = TurnResult(error=f"Something went wrong inside the assistant: {type(exc).__name__}: {exc}")
            finally:
                self._task = None
                self._fresh = False
                self._last_activity = time.time()
                bus.emit("state", processing=False)

            if reply.ignored:
                bus.emit("assistant_ignored")
                return TurnResult()
            if result.text:
                store.add_transcript(self.conversation_id, "assistant", result.text)
            bus.emit(
                "assistant_message", text=result.text, error=result.error, interrupted=result.interrupted,
                usage=result.usage, source=source,
            )
            return result

    async def interrupt(self, reason: str = "interrupted") -> None:
        """Stop the request in progress. Safe to call at any time."""
        task = self._task
        if task is None or task.done():
            return
        self._interrupting = True
        await self.backend.interrupt()
        with contextlib.suppress(Exception):
            await asyncio.wait({task}, timeout=4.0)
        if not task.done():
            task.cancel()                # the backend did not stop by itself: force it and start clean
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait({task}, timeout=2.0)
            await self.backend.reset()
