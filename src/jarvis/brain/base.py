"""What a reasoning backend must provide.

A backend owns one conversation with a model: it is given the user's words,
runs the model's tool calls through the shared `ToolExecutor`, streams the
reply back and can be interrupted at any moment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis.tools import Services, ToolExecutor, TurnState

TextSink = Callable[[str], None]
PromptSource = Callable[[], str]


class BackendUnavailable(Exception):
    """The backend cannot run as configured; the message tells the user how to fix it."""


@dataclass
class TurnResult:
    text: str = ""
    error: str | None = None          # a failure, phrased for the user
    interrupted: bool = False
    usage: dict[str, Any] = field(default_factory=dict)


class AgentBackend(ABC):
    name: str = "backend"

    def __init__(self, services: Services, executor: ToolExecutor, system_prompt: PromptSource) -> None:
        self.services = services
        self.executor = executor
        self.system_prompt = system_prompt

    @property
    @abstractmethod
    def label(self) -> str:
        """Human-readable description, e.g. 'Claude (haiku) on your Claude plan'."""

    async def start(self) -> None:  # noqa: B027 - optional hook
        """Check prerequisites. Raise BackendUnavailable with a helpful message if they are missing."""

    @abstractmethod
    async def run_turn(self, prompt: str, turn: TurnState, on_text: TextSink) -> TurnResult:
        """Answer one request, streaming reply text to `on_text`."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop the request in progress; `run_turn` should return promptly with interrupted=True."""

    @abstractmethod
    async def reset(self) -> None:
        """Forget the conversation so the next request starts a fresh one."""

    async def close(self) -> None:
        await self.reset()

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "label": self.label}
