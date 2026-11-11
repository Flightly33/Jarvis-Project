"""A stand-in backend with no AI model behind it.

It recognises a handful of fixed phrases and calls the real tools for them,
which is enough to try the interface, the approval flow and voice without
signing in to anything. Select it with `brain.backend = "demo"`.
"""

from __future__ import annotations

import asyncio
import json
import re

from jarvis.brain.base import AgentBackend, TextSink, TurnResult
from jarvis.tools import TurnState

_HELP = (
    "Demo mode is running without an AI model, so I can only show how the interface behaves. "
    "Try: list files; run git status; create file notes.txt saying hello; delete notes.txt; "
    "or remind me in 1 minute to stretch."
)


class DemoBackend(AgentBackend):
    name = "demo"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._task: asyncio.Task | None = None
        self._interrupted = False

    @property
    def label(self) -> str:
        return "Demo mode (no AI model connected)"

    async def run_turn(self, prompt: str, turn: TurnState, on_text: TextSink) -> TurnResult:
        text = prompt.split("\n\n<context>")[0].strip().rstrip(".!?")
        self._task, self._interrupted = asyncio.current_task(), False
        said: list[str] = []
        try:
            reply = await self._act(text, turn)
            for word in reply.split(" "):
                said.append(word + " ")
                on_text(word + " ")
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            if not self._interrupted:
                raise
            return TurnResult(text="".join(said).strip(), interrupted=True)
        finally:
            self._task = None
        return TurnResult(text=reply)

    async def _act(self, text: str, turn: TurnState) -> str:
        async def call(tool: str, **args):
            outcome = await self.executor.execute(tool, args, turn)
            try:
                return outcome, json.loads(outcome.text)
            except ValueError:
                return outcome, {}

        low = text.lower()
        if re.match(r"(list|show)( my)? (files|folder)", low):
            out, data = await call("list_directory")
            names = [e["name"] for e in data.get("entries", [])][:8]
            return out.text if out.is_error else f"Your folder holds {data.get('count', 0)} item(s)" + (": " + ", ".join(names) + "." if names else ".")
        if m := re.match(r"run (.+)", text, re.IGNORECASE):
            out, data = await call("run_command", command=m.group(1))
            if out.is_error:
                return out.text
            first = (data.get("output") or "").strip().splitlines()[:3]
            return f"That finished with exit code {data.get('exit_code')}. " + " ".join(first)
        if m := re.match(r"(?:write|create)(?: a)? file (\S+)(?: saying (.+))?", text, re.IGNORECASE):
            out, data = await call("write_file", path=m.group(1), content=(m.group(2) or "Hello from Jarvis.") + "\n")
            return out.text if out.is_error else f"I've created {m.group(1)} and confirmed it is there."
        if m := re.match(r"delete (\S+)", text, re.IGNORECASE):
            out, data = await call("delete_path", path=m.group(1))
            return out.text if out.is_error else f"{m.group(1)} is in the trash; you can restore it from there."
        if m := re.match(r"remind me in (\d+) minutes? to (.+)", text, re.IGNORECASE):
            out, data = await call("create_reminder", text=m.group(2), in_minutes=float(m.group(1)))
            return out.text if out.is_error else f"I'll remind you to {m.group(2)} in {m.group(1)} minute(s)."
        if m := re.match(r"open (.+)", text, re.IGNORECASE):
            out, data = await call("open_application", name=m.group(1))
            return out.text if out.is_error else f"{data.get('application', m.group(1))} is open."
        return _HELP

    async def interrupt(self) -> None:
        if self._task is not None:
            self._interrupted = True
            self._task.cancel()

    async def reset(self) -> None:
        return None
