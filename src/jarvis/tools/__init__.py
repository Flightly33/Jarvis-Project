"""Everything Jarvis can do, as tools that the reasoning backends may call."""

from jarvis.tools.apps import APP_TOOLS
from jarvis.tools.base import (
    ImagePart,
    Services,
    Tool,
    ToolContext,
    ToolError,
    ToolInput,
    ToolRegistry,
    ToolResult,
    TurnState,
)
from jarvis.tools.executor import ToolExecutor, ToolOutcome
from jarvis.tools.files import FILE_TOOLS
from jarvis.tools.memory_tools import MEMORY_TOOLS
from jarvis.tools.reminders import REMINDER_TOOLS
from jarvis.tools.shell import SHELL_TOOLS, JobManager
from jarvis.tools.system import SYSTEM_TOOLS
from jarvis.tools.web import WEB_TOOLS


def build_registry(include_web: bool = False) -> ToolRegistry:
    """The standard tool set. `include_web` adds Jarvis's own web tools for backends without built-in ones."""
    tools = [*FILE_TOOLS, *SHELL_TOOLS, *APP_TOOLS, *SYSTEM_TOOLS, *MEMORY_TOOLS, *REMINDER_TOOLS]
    if include_web:
        tools += WEB_TOOLS
    return ToolRegistry(tools)


__all__ = [
    "ImagePart",
    "JobManager",
    "Services",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolExecutor",
    "ToolInput",
    "ToolOutcome",
    "ToolRegistry",
    "ToolResult",
    "TurnState",
    "build_registry",
]
