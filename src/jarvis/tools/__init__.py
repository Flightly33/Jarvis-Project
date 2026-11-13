"""Everything Jarvis can do, as tools that the reasoning backends may call."""

import sys

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
from jarvis.tools.browser import BROWSER_TOOLS
from jarvis.tools.calendar import CALENDAR_TOOLS
from jarvis.tools.email import EMAIL_TOOLS
from jarvis.tools.executor import ToolExecutor, ToolOutcome
from jarvis.tools.files import FILE_TOOLS
from jarvis.tools.images import IMAGE_TOOLS
from jarvis.tools.memory_tools import MEMORY_TOOLS
from jarvis.tools.phone import PHONE_TOOLS
from jarvis.tools.reminders import REMINDER_TOOLS
from jarvis.tools.shell import SHELL_TOOLS, JobManager
from jarvis.tools.system import SYSTEM_TOOLS
from jarvis.tools.tabs import TAB_TOOLS
from jarvis.tools.vault import VAULT_TOOLS
from jarvis.tools.web import WEB_TOOLS

if sys.platform == "win32":
    from jarvis.tools.apps_windows import APP_TOOLS as _APPS
    from jarvis.tools.desktop_control import CONTROL_TOOLS
    from jarvis.tools.windows_input import WINDOW_TOOLS

    APP_TOOLS = [*_APPS, *WINDOW_TOOLS, *CONTROL_TOOLS]
else:
    from jarvis.tools.apps import APP_TOOLS


def build_registry(include_web: bool = False) -> ToolRegistry:
    """The standard tool set. `include_web` adds Jarvis's own web tools for backends without built-in ones."""
    tools = [*FILE_TOOLS, *SHELL_TOOLS, *APP_TOOLS, *SYSTEM_TOOLS, *MEMORY_TOOLS, *REMINDER_TOOLS, *CALENDAR_TOOLS, *BROWSER_TOOLS, *EMAIL_TOOLS, *TAB_TOOLS, *PHONE_TOOLS, *IMAGE_TOOLS, *VAULT_TOOLS]
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
