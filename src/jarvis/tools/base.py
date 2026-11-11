"""The contract every tool follows.

A tool is a typed input model, an `assess` step that says what the call would
do (for the safety policy and the approval dialog) and a `run` step that does
it. Tools never decide for themselves whether they may run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from jarvis.config import Config, expand_dirs
from jarvis.events import EventBus
from jarvis.memory import Store
from jarvis.paths import AppPaths
from jarvis.safety import ApprovalBroker, Assessment, ConversationFlags, EmergencyStop, PathSandbox, PolicyEngine

if TYPE_CHECKING:
    from jarvis.tools.shell import JobManager


class ToolInput(BaseModel):
    """Base class for tool arguments: unknown fields are rejected, strings are trimmed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolError(Exception):
    """A failure the model should hear about and can react to."""


@dataclass
class ImagePart:
    mime_type: str
    data: bytes


@dataclass
class ToolResult:
    data: Any = None                       # JSON-serialisable payload (or plain text) for the model
    summary: str = ""                      # one line for the activity log
    images: list[ImagePart] = field(default_factory=list)
    is_error: bool = False
    untrusted_source: str | None = None    # set when `data` came from outside (web page, mail...)


@dataclass
class TurnState:
    """One request from the user, from first word to final answer."""

    source: str = "text"                   # voice | text | system
    conversation_id: str = ""
    flags: ConversationFlags = field(default_factory=ConversationFlags)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    grants: set[str] = field(default_factory=set)
    started_at: float = field(default_factory=time.time)
    tool_calls: int = 0


@dataclass
class Services:
    """Shared machinery handed to every tool."""

    config: Config
    paths: AppPaths
    store: Store
    bus: EventBus
    approvals: ApprovalBroker
    estop: EmergencyStop
    sandbox: PathSandbox = field(init=False)
    policy: PolicyEngine = field(init=False)
    jobs: JobManager | None = None
    save_config: Callable[[], None] = lambda: None

    def __post_init__(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        """Rebuild whatever is derived from the configuration (after a settings change)."""
        safety = self.config.safety
        self.sandbox = PathSandbox(
            approved=expand_dirs(safety.approved_dirs),
            read_only=expand_dirs(safety.read_only_dirs),
            protected=self.paths.protected(),
        )
        self.policy = PolicyEngine(safety.autonomy, safety.exfiltration_guard)
        self.approvals.timeout_seconds = safety.approval_timeout_seconds


@dataclass
class ToolContext:
    services: Services
    turn: TurnState

    @property
    def config(self) -> Config:
        return self.services.config

    @property
    def sandbox(self) -> PathSandbox:
        return self.services.sandbox

    @property
    def store(self) -> Store:
        return self.services.store


RunFn = Callable[[Any, ToolContext], Awaitable[ToolResult]]
AssessFn = Callable[[Any, ToolContext], Assessment]


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[ToolInput]
    run: RunFn
    assess: AssessFn | None = None
    timeout: float | None = 60.0
    group: str = "general"

    def schema(self) -> dict[str, Any]:
        """JSON Schema for the arguments, trimmed of noise that costs tokens."""
        schema = _clean(self.input_model.model_json_schema())
        schema.pop("description", None)
        schema.setdefault("properties", {})
        schema["type"] = "object"
        schema["additionalProperties"] = False
        return schema


def _clean(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _clean(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return node


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
