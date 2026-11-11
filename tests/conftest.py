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
