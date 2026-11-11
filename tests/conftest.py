from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.paths import AppPaths
from jarvis.safety.sandbox import PathSandbox


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated home directory so nothing touches the real one."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))      # what Path.home() reads on Windows
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis-home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return h


@pytest.fixture
def paths(tmp_path: Path, home: Path) -> AppPaths:
    p = AppPaths(config_dir=tmp_path / "jarvis-home" / "config", data_dir=tmp_path / "jarvis-home" / "data")
    p.ensure()
    return p


@pytest.fixture
def workspace(home: Path) -> Path:
    w = home / "Jarvis"
    w.mkdir()
    return w


@pytest.fixture
def sandbox(home: Path, workspace: Path, paths: AppPaths) -> PathSandbox:
    docs = home / "Documents"
    docs.mkdir()
    return PathSandbox(approved=[workspace], read_only=[docs], protected=paths.protected(), home=home)


# --------------------------------------------------------------------------
# Shared machinery for tool and agent tests
# --------------------------------------------------------------------------

import asyncio  # noqa: E402
import contextlib  # noqa: E402

from jarvis.config import Config  # noqa: E402
from jarvis.events import EventBus  # noqa: E402
from jarvis.memory import Store  # noqa: E402
from jarvis.safety import ApprovalBroker, EmergencyStop  # noqa: E402
from jarvis.tools import JobManager, Services, ToolExecutor, TurnState, build_registry  # noqa: E402


@pytest.fixture
def config(home: Path, workspace: Path) -> Config:
    cfg = Config()
    cfg.safety.approved_dirs = [str(workspace)]
    cfg.safety.approval_timeout_seconds = 5
    return cfg


@pytest.fixture
def services(config: Config, paths: AppPaths):
    bus = EventBus()
    svc = Services(
        config=config, paths=paths, store=Store(paths.database), bus=bus,
        approvals=ApprovalBroker(bus, timeout_seconds=5), estop=EmergencyStop(),
    )
    svc.jobs = JobManager(bus)
    saved: list[int] = []
    svc.save_config = lambda: saved.append(1)
    svc.saved = saved  # type: ignore[attr-defined]
    yield svc
    svc.store.close()


@pytest.fixture
def executor(services) -> ToolExecutor:
    return ToolExecutor(build_registry(include_web=True), services)


@pytest.fixture
def turn() -> TurnState:
    return TurnState(source="text", conversation_id="test")


@contextlib.asynccontextmanager
async def answering(services, approve: bool, scope: str = "once"):
    """Answer every approval request that appears while the block runs, recording what was asked."""
    asked: list[dict] = []
    q = services.bus.subscribe()

    async def responder():
        while True:
            event = await q.get()
            if event["type"] == "approval_request":
                asked.append(event["request"])
                services.approvals.resolve(event["request"]["id"], approve, scope)

    task = asyncio.create_task(responder())
    try:
        yield asked
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        services.bus.unsubscribe(q)
