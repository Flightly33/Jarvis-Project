"""Wires the pieces together: settings, storage, safety, tools, reasoning backend, conversation."""

from __future__ import annotations

import asyncio
import logging

from jarvis.agent import Assistant
from jarvis.brain.base import AgentBackend, BackendUnavailable
from jarvis.config import Config, load_config, save_config
from jarvis.events import EventBus
from jarvis.memory import Store
from jarvis.paths import AppPaths, default_paths
from jarvis.persona import build_system_prompt
from jarvis.safety import ApprovalBroker, EmergencyStop
from jarvis.scheduler import ReminderScheduler
from jarvis.tools import JobManager, Services, ToolExecutor, build_registry

log = logging.getLogger(__name__)


class JarvisCore:
    def __init__(self, paths: AppPaths | None = None, config: Config | None = None) -> None:
        self.paths = paths or default_paths()
        self.paths.ensure()
        self.config = config or load_config(self.paths)
        self.bus = EventBus()
        self.store = Store(self.paths.database)
        self.estop = EmergencyStop()
        self.services = Services(
            config=self.config, paths=self.paths, store=self.store, bus=self.bus,
            approvals=ApprovalBroker(self.bus, self.config.safety.approval_timeout_seconds), estop=self.estop,
        )
        self.services.save_config = lambda: save_config(self.paths, self.config)
        self.services.jobs = JobManager(self.bus)
        for folder in self.services.sandbox.approved:        # the default workspace should simply exist
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.warning("could not create approved folder %s", folder)
        backend_name = self.config.brain.backend
        # Claude Code brings its own web search; other backends use Jarvis's web tools.
        self.executor = ToolExecutor(build_registry(include_web=backend_name != "claude"), self.services)
        self.backend = self._make_backend(backend_name)
        self.assistant = Assistant(self.services, self.backend)
        self.estop.register("approvals", lambda reason: self.services.approvals.decline_all())
        self.estop.register("assistant", self.assistant.interrupt)
        self.estop.register("background commands", lambda reason: self.services.jobs.stop_all())
        self.scheduler = ReminderScheduler(self.store, self.bus)
        self._started = False

    def _system_prompt(self) -> str:
        return build_system_prompt(self.config, self.services.sandbox, self.store)

    def _make_backend(self, name: str) -> AgentBackend:
        if name == "claude":
            from jarvis.brain.claude_code import ClaudeCodeBackend

            return ClaudeCodeBackend(self.services, self.executor, self._system_prompt)
        if name == "demo":
            from jarvis.brain.demo import DemoBackend

            return DemoBackend(self.services, self.executor, self._system_prompt)
        raise BackendUnavailable(
            f"The '{name}' reasoning backend is not available in this build yet. Set brain.backend = \"claude\" "
            f"in {self.paths.config_file}."
        )

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.bus.bind_loop(asyncio.get_running_loop())
        await self.backend.start()
        self.scheduler.start()

    async def apply_settings(self, changes: dict) -> None:
        """Apply a validated settings change from the interface and make it take effect."""
        persona_changed = False
        for section, values in changes.items():
            target = getattr(self.config, section)
            for key, value in values.items():
                if getattr(target, key) != value:
                    setattr(target, key, value)          # validated on assignment by the config model
                    persona_changed |= section in {"user", "claude"} or key in {"approved_dirs", "read_only_dirs"}
        save_config(self.paths, self.config)
        self.services.refresh()
        if persona_changed:
            # the model's standing instructions are fixed per conversation, so start a new one
            await self.assistant.new_conversation()
        self.bus.emit("settings_changed")

    async def emergency_stop(self, reason: str = "requested") -> None:
        failed = await self.estop.trigger(reason)
        self.bus.emit("estop", reason=reason, failed=failed)

    async def close(self) -> None:
        await self.scheduler.stop()
        await self.services.jobs.stop_all()
        await self.backend.close()
        self.store.close()
